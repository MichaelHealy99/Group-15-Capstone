import numpy as np
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.utils.data.dataset import Dataset
from torch.utils.data import DataLoader
import time, sys, os
import matplotlib.pyplot as plt

#from MaxViT
import tensorflow as tf
import maxvit

# my modules
from learnable_wavelets.models.models_factory import baseModelFactory, topModelFactory
from learnable_wavelets.models.sn_hybrid_models import sn_HybridModel
from learnable_wavelets.models.camels_models import get_architecture
from learnable_wavelets.camels.camels_dataset import *

camels_path="/mnt/ceph/users/camels/PUBLIC_RELEASE/CMD/2D_maps/data/"
fparams    = camels_path+"/params_IllustrisTNG.txt"
fmaps      = [
              "/mnt/home/cpedersen/ceph/Data/CAMELS_test/15k_fields/maps_Mtot.npy",
             ]
#fmaps      = [
#              "/mnt/home/cpedersen/ceph/Data/CAMELS_test/15k_fields/maps_Mtot.npy"         
#             ]
fmaps_norm      = [None]
splits          = 1
seed            = 123
monopole        = True  #keep the monopole of the maps (True) or remove it (False)
rot_flip_in_mem = False  #whether rotations and flipings are kept in memory
smoothing       = 0  ## Smooth the maps with a Gaussian filter? 0 for no
arch            = "sn" ## Which model architecture to use
features        = 12
name            = "sn_learnable" ## For wandb and optuna

## training parameters
batch_size  = 32
epochs      = 200
num_workers = 10    #number of workers to load data

## CNN values
# get the value of the hyperparameters
lr     = 1e-4
wd     = 0.01
dr     = 0.1
lr_sn  = 0.005
hidden=6 ## Number of hidden layers in the top model


# use GPUs if available
if torch.cuda.is_available():
    print("CUDA Available")
    device = torch.device('cuda')
else:
    print('CUDA Not Available')
    device = torch.device('cpu')
cudnn.benchmark = True      #May train faster but cost more memory

# training parameters
channels        = len(fmaps)
## Set up params
if features==2:
    g=[0,1]
if features==4:
    g=[0,1]
    h=[2,3]
elif features==6:
    g=[0,1,2,3,4,5]
elif features==12:
    g=[0,1,2,3,4,5]
    h=[6,7,8,9,10,11]

rot_flip_in_mem = False            #whether rotations and flipings are kept in memory. True will make the code faster but consumes more RAM memory.

config = {"learning rate": lr,
            "scattering learning rate": lr_sn,
            "wd": wd,
            "channels": channels,
            "epochs": epochs,
            "batch size": batch_size,
            "network": arch,
            "features": features,
            "splits":splits}


## Set number of classes for scattering network to output

# optimizer parameters
beta1 = 0.5
beta2 = 0.999
#######################################################################################################
#######################################################################################################

# get training set
print('\nPreparing training set')
train_loader = create_dataset_multifield('train', seed, fmaps, fparams, batch_size, splits, fmaps_norm,
                                            num_workers=num_workers, rot_flip_in_mem=rot_flip_in_mem, verbose=True)

# get validation set
print('\nPreparing validation set')
valid_loader = create_dataset_multifield('valid', seed, fmaps, fparams, batch_size, splits, fmaps_norm,
                                            num_workers=num_workers, rot_flip_in_mem=rot_flip_in_mem,  verbose=True)    

# get test set
print('\nPreparing test set')
test_loader = create_dataset_multifield('test', seed, fmaps, fparams, batch_size, splits, fmaps_norm,
                                        num_workers=num_workers, rot_flip_in_mem=rot_flip_in_mem,  verbose=True)

num_train_maps=len(train_loader.dataset.x)


if arch=="sn":
    ## First create a scattering network object
    scatteringBase = baseModelFactory( #creat scattering base model
        architecture='scattering',
        J=2,
        N=256,
        M=256,
        channels=channels,
        max_order=2,
        initialization="Random",
        seed=234,
        learnable=False,
        lr_orientation=lr_sn,
        lr_scattering=lr_sn,
        skip=True,
        split_filters=True,
        filter_video=False,
        subsample=4,
        device=device,
        use_cuda=True,
        plot=False
    )

    ## Now create a network to follow the scattering layers
    ## can be MLP, linear, or cnn at the moment
    ## (as in https://github.com/bentherien/ParametricScatteringNetworks/ )
    top = topModelFactory( #create cnn, mlp, linear_layer, or other
        base=scatteringBase,
        architecture="cnn",
        num_classes=features,
        width=hidden,
        average=True,
        use_cuda=True
    )

    ## Merge these into a hybrid model
    hybridModel = sn_HybridModel(scatteringBase=scatteringBase, top=top, use_cuda=True)
    model=hybridModel
    print("scattering layer + cnn set up")
elif arch=='maxvit':
    model = maxvit.MaxViT(
        image_size=256,
        patch_size=32,
        num_classes=features,
        dim=1024,
        depth=6,
        heads=16,
        mlp_dim=2048,
        dropout=0.1,
        emb_dropout=0.1,
        channels=channels)
    print("maxvit set up")
else:
    print("setting up model %s" % arch)
    model = get_architecture(arch,features,hidden,dr,channels)
model.to(device=device)


optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=wd, betas=(beta1, beta2))
scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min', factor=0.3, patience=10)

# do a loop over all epochs
start = time.time()
for epoch in range(epochs):
    log_dic={}
    if arch=="sn":
        wave_params=hybridModel.scatteringBase.params_filters
        orientations=wave_params[0].cpu().detach().numpy()
        xis=wave_params[1].cpu().detach().numpy()
        sigmas=wave_params[2].cpu().detach().numpy()
        slants=wave_params[3].cpu().detach().numpy()
        for aa in range(len(orientations)):
            log_dic["orientation_%d" % aa]=orientations[aa]
            log_dic["xi_%d" % aa]=xis[aa]
            log_dic["sigma_%d" % aa]=sigmas[aa]
            log_dic["slant_%d" % aa]=slants[aa]

    # train
    train_loss1, train_loss2 = torch.zeros(len(g)).to(device), torch.zeros(len(g)).to(device)
    train_loss, points = 0.0, 0
    model.train()
    for x, y in train_loader:
        bs   = x.shape[0]         #batch size
        x    = x.to(device)       #maps
        y    = y.to(device)[:,g]  #parameters
        p    = model(x)           #NN output
        y_NN = p[:,g]             #posterior mean
        loss1 = torch.mean((y_NN - y)**2,                axis=0)
        if features==4 or features==12:
            e_NN = p[:,h]         #posterior std
            loss2 = torch.mean(((y_NN - y)**2 - e_NN**2)**2, axis=0)
            loss  = torch.mean(torch.log(loss1) + torch.log(loss2))
            train_loss2 += loss2*bs
        else:
            loss = torch.mean(torch.log(loss1))
        train_loss1 += loss1*bs
        points      += bs
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

    train_loss = torch.log(train_loss1/points) 
    if features==4 or features==12:
        train_loss+=torch.log(train_loss2/points)
    train_loss = torch.mean(train_loss).item()

    # do validation: cosmo alone & all params
    valid_loss1, valid_loss2 = torch.zeros(len(g)).to(device), torch.zeros(len(g)).to(device)
    valid_loss, points = 0.0, 0
    model.eval()
    for x, y in valid_loader:
        with torch.no_grad():
            bs    = x.shape[0]         #batch size
            x     = x.to(device)       #maps
            y     = y.to(device)[:,g]  #parameters
            p     = model(x)           #NN output
            y_NN  = p[:,g]             #posterior mean
            loss1 = torch.mean((y_NN - y)**2,                axis=0)
            if features==4 or features==12:    
                e_NN  = p[:,h]         #posterior std
                loss2 = torch.mean(((y_NN - y)**2 - e_NN**2)**2, axis=0)
                valid_loss2 += loss2*bs
            valid_loss1 += loss1*bs
            points     += bs


    valid_loss = torch.log(valid_loss1/points) 
    if features==4 or features==12:
        valid_loss+=torch.log(valid_loss2/points)
    valid_loss = torch.mean(valid_loss).item()

    scheduler.step(valid_loss)
    log_dic["training_loss"]=train_loss
    log_dic["valid_loss"]=valid_loss

    # verbose
    print('%03d %.3e %.3e '%(epoch, train_loss, valid_loss), end='')
    print("")

stop = time.time()
print('Time take (h):', "{:.4f}".format((stop-start)/3600.0))

## Model performance metrics on test set
num_maps=test_loader.dataset.size
## Now loop over test set and print accuracy
# define the arrays containing the value of the parameters
params_true = np.zeros((num_maps,len(g)), dtype=np.float32)
params_NN   = np.zeros((num_maps,len(g)), dtype=np.float32)
errors_NN   = np.zeros((num_maps,len(g)), dtype=np.float32)

# get test loss
test_loss1, test_loss2 = torch.zeros(len(g)).to(device), torch.zeros(len(g)).to(device)
test_loss, points = 0.0, 0
model.eval()
for x, y in test_loader:
    with torch.no_grad():
        bs    = x.shape[0]         #batch size
        x     = x.to(device)       #send data to device
        y     = y.to(device)[:,g]  #send data to device
        p     = model(x)           #prediction for mean and variance
        y_NN  = p[:,g]             #prediction for mean
        loss1 = torch.mean((y_NN - y)**2,                axis=0)
        if features==4 or features==12:
            e_NN  = p[:,h]         #posterior std
            loss2 = torch.mean(((y_NN - y)**2 - e_NN**2)**2, axis=0)
            test_loss2 += loss2*bs
        test_loss1 += loss1*bs
        test_loss = torch.log(test_loss1/points)
        if features==4 or features==12:
            test_loss+=torch.log(test_loss2/points)
        test_loss = torch.mean(test_loss).item()

#e_NN  = p[:,6:]       #prediction for error
#loss1 = torch.mean((y_NN[:,g] - y[:,g])**2,                     axis=0)
#loss2 = torch.mean(((y_NN[:,g] - y[:,g])**2 - e_NN[:,g]**2)**2, axis=0)
#test_loss1 += loss1*bs
#test_loss2 += loss2*bs

        # save results to their corresponding arrays
        params_true[points:points+x.shape[0]] = y.cpu().numpy() 
        params_NN[points:points+x.shape[0]]   = y_NN.cpu().numpy()
        if features==4 or features==12:
            errors_NN[points:points+x.shape[0]]   = e_NN.cpu().numpy()
        points    += x.shape[0]
test_loss = torch.log(test_loss1/points) + torch.log(test_loss2/points)
test_loss = torch.mean(test_loss).item()
print('Test loss = %.3e\n'%test_loss)

# de-normalize
## I guess these are the hardcoded parameter limits
minimum = np.array([0.1, 0.6, 0.25, 0.25, 0.5, 0.5])
maximum = np.array([0.5, 1.0, 4.00, 4.00, 2.0, 2.0])

## Drop feedback parameters if they aren't included
minimum=minimum[g]
maximum=maximum[g]
params_true = params_true*(maximum - minimum) + minimum
params_NN   = params_NN*(maximum - minimum) + minimum


test_error = 100*np.mean(np.sqrt((params_true - params_NN)**2)/params_true,axis=0)
print('Error Omega_m = %.3f'%test_error[0])
print('Error sigma_8 = %.3f'%test_error[1])

if features>4:
    print('Error A_SN1   = %.3f'%test_error[2])
    print('Error A_AGN1  = %.3f'%test_error[3])
    print('Error A_SN2   = %.3f'%test_error[4])
    print('Error A_AGN2  = %.3f\n'%test_error[5])


if features==4:
    errors_NN   = errors_NN*(maximum - minimum)
    mean_error = 100*(np.absolute(np.mean(errors_NN/params_NN, axis=0)))
    print('Bayesian error Omega_m = %.3f'%mean_error[0])
    print('Bayesian error sigma_8 = %.3f'%mean_error[1])


elif features==12:
    errors_NN   = errors_NN*(maximum - minimum)
    mean_error = 100*(np.absolute(np.mean(errors_NN/params_NN, axis=0)))
    print('Bayesian error Omega_m = %.3f'%mean_error[0])
    print('Bayesian error sigma_8 = %.3f'%mean_error[1])
    print('Bayesian error A_SN1   = %.3f'%mean_error[2])
    print('Bayesian error A_AGN1  = %.3f'%mean_error[3])
    print('Bayesian error A_SN2   = %.3f'%mean_error[4])
    print('Bayesian error A_AGN2  = %.3f\n'%mean_error[5])



if features<5:
    f, axarr = plt.subplots(1, 2, figsize=(9,6))
    axarr[0].plot(np.linspace(min(params_true[:,0]),max(params_true[:,0]),100),np.linspace(min(params_true[:,0]),max(params_true[:,0]),100),color="black")
    axarr[1].plot(np.linspace(min(params_true[:,1]),max(params_true[:,1]),100),np.linspace(min(params_true[:,1]),max(params_true[:,1]),100),color="black")
    if features==4:
        axarr[0].errorbar(params_true[:,0],params_NN[:,0],errors_NN[:,0],marker="o",ls="none")
        axarr[1].errorbar(params_true[:,1],params_NN[:,1],errors_NN[:,1],marker="o",ls="none")
    else:
        axarr[0].plot(params_true[:,0],params_NN[:,0],marker="o",ls="none")
        axarr[1].plot(params_true[:,1],params_NN[:,1],marker="o",ls="none")
        
    axarr[0].set_xlabel(r"True $\Omega_m$")
    axarr[0].set_ylabel(r"Predicted $\Omega_m$")
    axarr[0].text(0.1,0.9,"%.3f %% error" % test_error[0],fontsize=12,transform=axarr[0].transAxes)

    axarr[1].set_xlabel(r"True $\sigma_8$")
    axarr[1].set_ylabel(r"Predicted $\sigma_8$")
    axarr[1].text(0.1,0.9,"%.3f %% error" % test_error[1],fontsize=12,transform=axarr[1].transAxes)


if features>4:
    f, axarr = plt.subplots(3, 2, figsize=(14,20))
    for aa in range(0,6,2):
        axarr[aa//2][0].plot(np.linspace(min(params_true[:,aa]),max(params_true[:,aa]),100),np.linspace(min(params_true[:,aa]),max(params_true[:,aa]),100),color="black")
        axarr[aa//2][1].plot(np.linspace(min(params_true[:,aa+1]),max(params_true[:,aa+1]),100),np.linspace(min(params_true[:,aa+1]),max(params_true[:,aa+1]),100),color="black")
        if features==12:
            axarr[aa//2][0].errorbar(params_true[:,aa],params_NN[:,aa],errors_NN[:,aa],marker="o",ls="none")
            axarr[aa//2][1].errorbar(params_true[:,aa+1],params_NN[:,aa+1],errors_NN[:,aa+1],marker="o",ls="none")
        else:
            axarr[aa//2][0].plot(params_true[:,aa],params_NN[:,aa],marker="o",ls="none")
            axarr[aa//2][1].plot(params_true[:,aa+1],params_NN[:,aa+1],marker="o",ls="none")
            
    axarr[0][0].set_xlabel(r"True $\Omega_m$")
    axarr[0][0].set_ylabel(r"Predicted $\Omega_m$")
    axarr[0][0].text(0.1,0.9,"%.3f %% error" % test_error[0],fontsize=12,transform=axarr[0][0].transAxes)

    axarr[0][1].set_xlabel(r"True $\sigma_8$")
    axarr[0][1].set_ylabel(r"Predicted $\sigma_8$")
    axarr[0][1].text(0.1,0.9,"%.3f %% error" % test_error[1],fontsize=12,transform=axarr[0][1].transAxes)

    axarr[1][0].set_xlabel(r"True $A_\mathrm{SN1}$")
    axarr[1][0].set_ylabel(r"Predicted $A_\mathrm{SN1}$")
    axarr[1][0].text(0.1,0.9,"%.3f %% error" % test_error[2],fontsize=12,transform=axarr[1][0].transAxes)

    axarr[1][1].set_xlabel(r"True $A_\mathrm{AGN1}$")
    axarr[1][1].set_ylabel(r"Predicted $A_\mathrm{AGN1}$")
    axarr[1][1].text(0.1,0.9,"%.3f %% error" % test_error[3],fontsize=12,transform=axarr[1][1].transAxes)

    axarr[2][0].set_xlabel(r"True $A_\mathrm{SN2}$")
    axarr[2][0].set_ylabel(r"Predicted $A_\mathrm{SN2}$")
    axarr[2][0].text(0.1,0.9,"%.3f %% error" % test_error[4],fontsize=12,transform=axarr[2][0].transAxes)

    axarr[2][1].set_xlabel(r"True $A_\mathrm{AGN2}$")
    axarr[2][1].set_ylabel(r"Predicted $A_\mathrm{AGN2}$")
    axarr[2][1].text(0.1,0.9,"%.3f %% error" % test_error[4],fontsize=12,transform=axarr[2][1].transAxes)


