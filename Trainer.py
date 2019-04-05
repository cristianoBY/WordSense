import sys
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D  

from torch.nn import Parameter
from torch.nn import MSELoss, L1Loss, SmoothL1Loss, CrossEntropyLoss, CosineEmbeddingLoss
from torch.distributions.binomial import Binomial
import torch.nn.utils.rnn as rnn_utils

import pandas as pd
import numpy as np
import math
from scipy.stats import spearmanr, pearsonr
from sklearn.metrics import precision_score, f1_score, recall_score

from tqdm import tqdm
from tqdm import tqdm_notebook as tqdm_n

from collections import Iterable, defaultdict
import itertools

from allennlp.modules.elmo import Elmo, batch_to_ids
from allennlp.commands.elmo import ElmoEmbedder
from model import *

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
print('Device: {}'.format(device))

class Trainer(object):

	def __init__(self, 
				optimizer_class = torch.optim.Adam,
				optim_wt_decay = 0.,
				epochs = 5,
				train_batch_size = 64,
				data_name = None,
				pretrain_data_name = None,
				predict_batch_size = 128,
				pretraining = False,
				regularization = None,
				loss_type = 'cos',
				pdist = torch.nn.PairwiseDistance(p = 2), # norm distance between 2 vectors
				all_senses = None,
				elmo_class = None, # for sense vector in the model
				file_path = "",
				device = device,
				**kwargs):

		## Training parameters
		self.epochs = epochs
		self.elmo_class = elmo_class
		self.pdist = pdist
		self.train_batch_size = train_batch_size
		self.predict_batch_size = predict_batch_size
		self.pretraining = pretraining
		self.data_name = data_name
		self.pretrain_data_name = pretrain_data_name

		## optimizer 
		self.sense_optimizer = optimizer_class
		self.def_optimizer = optimizer_class
		self.optim_wt_decay = optim_wt_decay
		
		# taget word index and senses list
		self.all_senses = all_senses

		self._init_kwargs = kwargs
		self.device = device

		# loss to calculate the similarity betwee two tensors
		if loss_type == 'mse':
			self.loss = MSELoss().to(self.device)
		else:
			self.loss = CosineEmbeddingLoss().to(self.device)
		
		'''
		if regularization == "l1":
			self.regularization = L1Loss()
		elif regularization == "smoothl1":
			self.regularization = SmoothL1Loss()
		else:
			self.regularization = None
		'''
		self.best_model_file =  file_path + "word_sense_model_.pth"		
		'''
		if self.regularization:
			self.regularization = self.regularization.to(self.device)
		'''

	# generate new model
	def _initialize_trainer_model(self):
		self._model = Model(device = self.device, all_senses = self.all_senses, elmo_class = self.elmo_class)
		self._model = self._model.to(self.device)

		print("#############   Model Parameters   ##############")
		for name, param in self._model.named_parameters():     
			if param.requires_grad:
				print(name, param.size())
		print("##################################################")

	def _custom_loss(self):
		'''
		generate custom loss to both
		pull closer correct sense vector
		push away wrong definitions
		'''
		_custom_loss = []

		return _custom_loss

	
	def train(self, train_X, train_Y, train_idx, dev_X, dev_Y, dev_idx, development = False, **kwargs):

		# train_Y is the annotator response
		self.train_X, self.train_Y = train_X, train_Y
		self.dev_X, self.dev_Y = dev_X, dev_Y
			
		self._initialize_trainer_model()  
		# print(self._model.definition_embeddings['spring'][0])

		# trainer setup
		parameters = [p for p in self._model.parameters() if p.requires_grad]
		sense_optimizer = self.sense_optimizer(parameters, weight_decay = self.optim_wt_decay, **kwargs)
		num_train = len(self.train_X)
		# num_dev = len(self.dev_X)
		
		# dev_accs = []
		best_loss = float('inf')
		best_r = -float('inf')
		train_losses = []
		dev_losses = []
		dev_rs = []
		bad_count = 0
		distance_all = []
		
		for epoch in range(self.epochs):
			
			# loss and distance for the cirrent iteration
			batch_losses = []
			distance = []

			# Turn on training mode which enables dropout.
			self._model.train()			
			tqdm.write("[Epoch: {}/{}]".format((epoch + 1), self.epochs))
			
			# time print
			pbar = tqdm_n(total = num_train)
			
			# SGD batch = 1
			for idx, sentence in enumerate(self.train_X):
				
				# Zero grad
				sense_optimizer.zero_grad()

				# the target word
				word_idx = train_idx[idx]
				word_lemma = sentence[word_idx]
				# print(word_lemma)

				# model output
				# cast single data point to 1-digit list for SGD to batch
				sen_list = []
				sen_list.append(sentence)
				word_idx_list = []
				word_idx_list.append(word_idx)
				sense_vec = self._model.forward(sen_list, word_idx_list)[0].view(self._model.output_size, -1)
				
				# calculate loss pair-wise: sense vector and definition vector
				# loss_positive = torch.zeros(()).to(self.device)
				# loss_negative = torch.zeros(()).to(self.device)
				losses = []

				# check all definitions in the annotator response for the target word
				for i, response in enumerate(self.train_Y[idx]):

					# carefully clone a leaf tensor for gradient calculation
					definition_vec_old = self._model.definition_embeddings[word_lemma][:, i].view(self._model.output_size, -1)
					definition_vec = definition_vec_old.clone().detach().requires_grad_(True)

					def_optimizer = self.def_optimizer([definition_vec], weight_decay = self.optim_wt_decay, **kwargs)
					def_optimizer.zero_grad()

					if response:

						definition_loss = self.loss(sense_vec, definition_vec, torch.ones(sense_vec.size()))
						losses.append(definition_loss)

						# backprop for this specific definition
						definition_loss.backward(definition_vec, retain_graph = True)
						distance.append(self.pdist(sense_vec, definition_vec))
					else:

						# if annotator response is False
						# increase the distance
						definition_loss = self.loss(sense_vec, definition_vec, -torch.ones(sense_vec.size()))
						losses.append(definition_loss)

						# backprop for this specific definition
						definition_loss.backward(definition_vec, retain_graph = True)

					# individual update for definition matrix
					def_optimizer.step()

					# put back to original definition matrix
					self._model.definition_embeddings[word_lemma][:, i] = definition_vec.view(self._model.output_size)

				# backprop for the predicted sense embeddings
				loss = sum(losses)
				loss.backward()

				# record training loss for each example
				current_loss = loss.detach().item()
				batch_losses.append(current_loss)

				sense_optimizer.step()
				pbar.update(1)
					
			pbar.close()
			
			# calculate the training loss of the current epoch
			curr_train_loss = np.mean(batch_losses)
			print("Epoch: {}, Mean Training Loss: {}".format(epoch + 1, curr_train_loss))

			# calculate the change in distance between correct sense and definition
			distance_all.append(torch.mean(torch.stack(distance)))

			# save the best model
			if curr_train_loss < best_loss:
				with open(self.best_model_file, 'wb') as f:
					torch.save(self._model.state_dict(), f)
				best_loss = curr_train_loss
			
			# early stopping
			if epoch:
				if (abs(curr_train_loss - train_losses[-1]) < 0.0001):
					break

			train_losses.append(curr_train_loss)

		# print(self._model.definition_embeddings['spring'][0])
		return train_losses, dev_losses, dev_rs, distance_all
