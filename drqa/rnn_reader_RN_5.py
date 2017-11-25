# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.
import torch
import torch.nn as nn
from . import layers_RN as layers

# Modification: add 'pos' and 'ner' features.
# Origin: https://github.com/facebookresearch/ParlAI/tree/master/parlai/agents/drqa

def normalize_emb_(data):
    print (data.size(), data[:10].norm(2,1))
    norms = data.norm(2,1) + 1e-8
    if norms.dim() == 1:
        norms = norms.unsqueeze(1)
    data.div_(norms.expand_as(data))
    print (data.size(), data[:10].norm(2,1))

class RnnDocReader(nn.Module):
    """Network for the Document Reader module of DrQA."""
    RNN_TYPES = {'lstm': nn.LSTM, 'gru': nn.GRU, 'rnn': nn.RNN}

    def __init__(self, opt, padding_idx=0, embedding=None, normalize_emb=False):
        super(RnnDocReader, self).__init__()
        # Store config
        self.opt = opt

        # Word embeddings
        if opt['pretrained_words']:
            assert embedding is not None
            self.embedding = nn.Embedding(embedding.size(0),
                                          embedding.size(1),
                                          padding_idx=padding_idx)
            if normalize_emb: normalize_emb_(embedding)
            self.embedding.weight.data = embedding

            if opt['fix_embeddings']:
                assert opt['tune_partial'] == 0
                for p in self.embedding.parameters():
                    p.requires_grad = False
            elif opt['tune_partial'] > 0:
                assert opt['tune_partial'] + 2 < embedding.size(0)
                fixed_embedding = embedding[opt['tune_partial'] + 2:]
                self.register_buffer('fixed_embedding', fixed_embedding)
                self.fixed_embedding = fixed_embedding
        else:  # random initialized
            self.embedding = nn.Embedding(opt['vocab_size'],
                                          opt['embedding_dim'],
                                          padding_idx=padding_idx)
        if opt['pos']:
            self.pos_embedding = nn.Embedding(opt['pos_size'], opt['pos_dim'])
            if normalize_emb: normalize_emb_(self.pos_embedding.weight.data)
        if opt['ner']:
            self.ner_embedding = nn.Embedding(opt['ner_size'], opt['ner_dim'])
            if normalize_emb: normalize_emb_(self.ner_embedding.weight.data)
        # Projection for attention weighted question
        if opt['use_qemb']:
            self.qemb_match = layers.SeqAttnMatch(opt['embedding_dim'])

        # Input size to RNN: word emb + question emb + manual features
        doc_input_size = opt['embedding_dim'] + opt['num_features']
        if opt['use_qemb']:
            doc_input_size += opt['embedding_dim']
        if opt['pos']:
            doc_input_size += opt['pos_dim']
        if opt['ner']:
            doc_input_size += opt['ner_dim']

        # RNN document encoder
        self.doc_rnn = layers.StackedBRNN(
            input_size=doc_input_size,
            hidden_size=opt['hidden_size'],
            num_layers=opt['doc_layers'],
            dropout_rate=opt['dropout_rnn'],
            dropout_output=opt['dropout_rnn_output'],
            concat_layers=opt['concat_rnn_layers'],
            rnn_type=self.RNN_TYPES[opt['rnn_type']],
            padding=opt['rnn_padding'],
        )

        # RNN question encoder
        self.question_rnn = layers.StackedBRNN(
            input_size=opt['embedding_dim'],
            hidden_size=opt['hidden_size'],
            num_layers=opt['question_layers'],
            dropout_rate=opt['dropout_rnn'],
            dropout_output=opt['dropout_rnn_output'],
            concat_layers=opt['concat_rnn_layers'],
            rnn_type=self.RNN_TYPES[opt['rnn_type']],
            padding=opt['rnn_padding'],
        )

        # Output sizes of rnn encoders
        doc_hidden_size = 2 * opt['hidden_size']
        question_hidden_size = 2 * opt['hidden_size']
        if opt['concat_rnn_layers']:
            doc_hidden_size *= opt['doc_layers']
            question_hidden_size *= opt['question_layers']

        num_ojbects = opt['num_objects']
        reduction_ratio = opt['reduction_ratio']
        self.doc_layerNorm1 = layers.LayerNorm(d_hid=doc_hidden_size)
        self.doc_1by1conv = layers.Conv1by1DimReduce(in_channels=doc_hidden_size,out_channels=doc_hidden_size//reduction_ratio)
        #self.doc_layerNorm2 = layers.LayerNorm(d_hid=doc_hidden_size//reduction_ratio)
        #self.doc_conv_encoder = layers.convEncoder(in_channels=doc_hidden_size//reduction_ratio,out_channels=doc_hidden_size//reduction_ratio)
        self.doc_self_attn = layers.doc_LinearSeqAttn(input_size=doc_hidden_size//reduction_ratio,output_size=num_ojbects)
        #self.doc_layerNorm3 = layers.LayerNorm(d_hid=doc_hidden_size // reduction_ratio)
        self.question_1by1conv = layers.Conv1by1DimReduce(in_channels=question_hidden_size, out_channels=question_hidden_size// reduction_ratio)
        self.question_layerNorm1 = layers.LayerNorm(d_hid=question_hidden_size // reduction_ratio)

        # Question merging
        if opt['question_merge'] not in ['avg', 'self_attn']:
            raise NotImplementedError('question_merge = %s' % opt['question_merge'])
        if opt['question_merge'] == 'self_attn':
            self.self_attn = layers.LinearSeqAttn(question_hidden_size//reduction_ratio)
        self.question_layerNorm2 = layers.LayerNorm(d_hid=question_hidden_size // reduction_ratio)

        self.relationNet = layers.RelationNetwork(num_objects=num_ojbects, hidden_size=3 * doc_hidden_size//reduction_ratio,output_size=doc_hidden_size//reduction_ratio)

        # Bilinear attention for span start/end
        self.start_attn = layers.BilinearSeqAttn(
            doc_hidden_size//reduction_ratio,
            question_hidden_size//reduction_ratio,
        )
        self.end_attn = layers.BilinearSeqAttn(
            doc_hidden_size//reduction_ratio,
            question_hidden_size//reduction_ratio,
        )

    def forward(self, x1, x1_f, x1_pos, x1_ner, x1_mask, x2, x2_mask):
        """Inputs:
        x1 = document word indices             [batch * len_d]
        x1_f = document word features indices  [batch * len_d * nfeat]
        x1_pos = document POS tags             [batch * len_d]
        x1_ner = document entity tags          [batch * len_d]
        x1_mask = document padding mask        [batch * len_d]
        x2 = question word indices             [batch * len_q]
        x2_mask = question padding mask        [batch * len_q]
        """
        # Embed both document and question
        x1_emb = self.embedding(x1)
        x2_emb = self.embedding(x2)

        if self.opt['dropout_emb'] > 0:
            x1_emb = nn.functional.dropout(x1_emb, p=self.opt['dropout_emb'],
                                               training=self.training)
            x2_emb = nn.functional.dropout(x2_emb, p=self.opt['dropout_emb'],
                                           training=self.training)

        drnn_input_list = [x1_emb, x1_f]
        # Add attention-weighted question representation
        if self.opt['use_qemb']:
            x2_weighted_emb = self.qemb_match(x1_emb, x2_emb, x2_mask)
            drnn_input_list.append(x2_weighted_emb)
        if self.opt['pos']:
            x1_pos_emb = self.pos_embedding(x1_pos)
            if self.opt['dropout_emb'] > 0:
                x1_pos_emb = nn.functional.dropout(x1_pos_emb, p=self.opt['dropout_emb'],
                                               training=self.training)
            drnn_input_list.append(x1_pos_emb)
        if self.opt['ner']:
            x1_ner_emb = self.ner_embedding(x1_ner)
            if self.opt['dropout_emb'] > 0:
                x1_ner_emb = nn.functional.dropout(x1_ner_emb, p=self.opt['dropout_emb'],
                                               training=self.training)
            drnn_input_list.append(x1_ner_emb)
        drnn_input = torch.cat(drnn_input_list, 2)

        # Encode document with RNN
        doc_hiddens = self.doc_rnn(drnn_input, x1_mask)
        #doc_hiddens = self.doc_layerNorm1(doc_hiddens.view(-1,doc_hiddens.size(2))).view_as(doc_hiddens)
        #if self.eval(): print('doc_hiddens:',doc_hiddens.size())
        doc_hiddens = self.doc_1by1conv(doc_hiddens)
        #if self.eval(): print('doc_hiddens:', doc_hiddens.size())
        #doc_hiddens_compact = self.doc_conv_encoder(doc_hiddens)
        #if self.eval(): print('doc_hiddens_compact:', doc_hiddens_compact.size())

        # doc_merge_weights = self.doc_self_attn(doc_hiddens_compact,x1_mask)
        doc_merge_weights = self.doc_self_attn(doc_hiddens,x1_mask)

        #if self.eval(): print('doc_merge_weights:', doc_merge_weights.size())

        #doc_hiddens_compact = torch.bmm(doc_merge_weights.transpose(1, 2), doc_hiddens_compact)
        doc_hiddens_compact = torch.bmm(doc_merge_weights.transpose(1, 2), doc_hiddens)

        #if self.eval(): print('doc_hiddens_compact:', doc_hiddens_compact.size())

        # Encode question with RNN + merge hiddens
        question_hiddens = self.question_rnn(x2_emb, x2_mask)
        #question_hiddens = self.question_layerNorm1(question_hiddens.view(-1, question_hiddens.size(2))).view_as(question_hiddens)
        question_hiddens = self.question_1by1conv(question_hiddens)
        #if self.eval(): print('question_hiddens:', question_hiddens.size())

        if self.opt['question_merge'] == 'avg':
            q_merge_weights = layers.uniform_weights(question_hiddens, x2_mask)
        elif self.opt['question_merge'] == 'self_attn':
            q_merge_weights = self.self_attn(question_hiddens, x2_mask)
        question_hidden = layers.weighted_avg(question_hiddens, q_merge_weights)
        #question_hidden = self.question_layerNorm2(question_hidden)
        #if self.eval(): print('question_hidden:', question_hidden.size())

        '''
        Relation Network
        '''
        doc_question_hidden = self.relationNet(doc_hiddens_compact,question_hidden)
        #if self.eval(): print('doc_question_hidden:', doc_question_hidden.size())


        # Predict start and end positions
        start_scores = self.start_attn(doc_hiddens, doc_question_hidden, x1_mask)
        end_scores = self.end_attn(doc_hiddens, doc_question_hidden, x1_mask)
        return start_scores, end_scores
