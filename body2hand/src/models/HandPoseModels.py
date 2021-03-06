import math

import torch
import torch.nn as nn

from fairseq import utils
from fairseq.models.fairseq_encoder import EncoderOut
from fairseq.modules import (
    FairseqDropout,
    LayerDropModuleList,
    LayerNorm,
    PositionalEmbedding, #Implemented my own
    SinusoidalPositionalEmbedding,
    TransformerEncoderLayer,
)

class ConvModel(nn.Module):
    def __init__(self, conv_channels, activation, pos_emb):

        super(ConvModel, self).__init__()
        if pos_emb:
            #self.pos_emb = PositionalEncoding(2*12, dropout=0.0, max_len=100)
            self.pos_emb = LinearPositionalEmbedding(max_len=100)
            self.conv1 = nn.Conv1d(12 * 2 + 1, conv_channels, kernel_size=5, padding=2)
            #self.conv1 = nn.Conv1d(12 * 2, conv_channels, kernel_size=5, padding=2)
        else:
            self.pos_emb = None
            self.conv1 = nn.Conv1d(12 * 2, conv_channels, kernel_size=5, padding=2)

        self.conv2 = nn.Conv1d(conv_channels, conv_channels, kernel_size=5, padding=2)
        self.conv3 = nn.Conv1d(conv_channels, conv_channels, kernel_size=5, padding=2)
        self.conv4 = nn.Conv1d(conv_channels, 2 * 21, kernel_size=5, padding=2)

        if activation == "ReLU":
            self.activation = nn.ReLU()
        else:
            raise ValueError()


    def forward(self, inp):


        inp = inp.permute(0, 2, 3, 1)
        bs, n_keypoints, dim, len = inp.shape

        inp = inp.view(bs, n_keypoints * dim, len)

        if self.pos_emb:
            # inp = inp.permute(0, 2, 1)
            # inp = self.pos_emb(inp)
            # inp = inp.permute(0, 2, 1)

            inp = self.pos_emb(inp, None)

        out = self.activation(self.conv1(inp))
        out = self.activation(self.conv2(out))
        out = self.activation(self.conv3(out))
        out = self.conv4(out)

        out = out.view(bs, -1, dim, len)

        out = out.permute(0, 3, 1, 2)

        return out


class TransformerEncoder(nn.Module):
    """
    Transformer encoder consisting of *args.encoder_layers* layers. Each layer
    is a :class:`TransformerEncoderLayer`.
    Args:
        args (argparse.Namespace): parsed command-line arguments
        dictionary (~fairseq.data.Dictionary): encoding dictionary
        embed_tokens (torch.nn.Embedding): input embedding
    """

    def __init__(self, args, embed_dim):

        super(TransformerEncoder, self).__init__()

        self.dropout_module = FairseqDropout(args.dropout,
                                             module_name=self.__class__.__name__)
        self.encoder_layerdrop = args.encoder_layerdrop

        # embed_dim = embed_tokens.embedding_dim
        # self.padding_idx = embed_tokens.padding_idx
        self.max_source_positions = args.max_source_positions

        # self.embed_tokens = embed_tokens

        # self.embed_scale = 1.0 if args.no_scale_embedding else math.sqrt(embed_dim)

        # self.embed_positions = (
        #     PositionalEmbedding(
        #         args.max_source_positions,
        #         embed_dim,
        #         self.padding_idx,
        #         learned=args.encoder_learned_pos,
        #     )
        #     if not args.no_token_positional_embeddings
        #     else None
        # )
        self.embed_positions = None

        if getattr(args, "layernorm_embedding", False):
            self.layernorm_embedding = LayerNorm(embed_dim)
        else:
            self.layernorm_embedding = None

        if not args.adaptive_input and args.quant_noise_pq > 0:
            self.quant_noise = apply_quant_noise_(
                nn.Linear(embed_dim, embed_dim, bias=False),
                args.quant_noise_pq,
                args.quant_noise_pq_block_size,
            )
        else:
            self.quant_noise = None

        if self.encoder_layerdrop > 0.0:
            self.layers = LayerDropModuleList(p=self.encoder_layerdrop)
        else:
            self.layers = nn.ModuleList([])


        self.layers.extend(
            [self.build_encoder_layer(args) for i in range(args.encoder_layers)]
        )
        self.num_layers = len(self.layers)

        if args.encoder_normalize_before:
            self.layer_norm = LayerNorm(embed_dim)
        else:
            self.layer_norm = None

    def build_encoder_layer(self, args):

        return TransformerEncoderLayer(args)


    def forward_embedding(self, src_tokens):
        # embed tokens and positions
        x = embed = self.embed_scale * self.embed_tokens(src_tokens)
        if self.embed_positions is not None:
            x = embed + self.embed_positions(src_tokens)
        if self.layernorm_embedding is not None:
            x = self.layernorm_embedding(x)
        x = self.dropout_module(x)
        if self.quant_noise is not None:
            x = self.quant_noise(x)
        return x, embed

    def forward(self, src_tokens, src_lengths,
                return_all_hiddens: bool = False):
        """
        Args:
            src_tokens (LongTensor): tokens in the source language of shape
                `(batch, src_len)`
            src_lengths (torch.LongTensor): lengths of each source sentence of
                shape `(batch)`
            return_all_hiddens (bool, optional): also return all of the
                intermediate hidden states (default: False).
        Returns:
            namedtuple:
                - **encoder_out** (Tensor): the last encoder layer's output of
                  shape `(src_len, batch, embed_dim)`
                - **encoder_padding_mask** (ByteTensor): the positions of
                  padding elements of shape `(batch, src_len)`
                - **encoder_embedding** (Tensor): the (scaled) embedding lookup
                  of shape `(batch, src_len, embed_dim)`
                - **encoder_states** (List[Tensor]): all intermediate
                  hidden states of shape `(src_len, batch, embed_dim)`.
                  Only populated if *return_all_hiddens* is True.
        """
        # x, encoder_embedding = self.forward_embedding(src_tokens)

        # B x T x C -> T x B x C
        x = src_tokens
        batch_size, seq_len = (int(x.shape[0]), int(x.shape[1]))
        x = x.view(batch_size, seq_len, -1)
        x = x.transpose(0, 1)

        # compute padding mask
        # encoder_padding_mask = src_tokens.eq(self.padding_idx)

        encoder_padding_mask = torch.zeros(batch_size, seq_len, dtype=torch.uint8)
        encoder_padding_mask += 1

        encoder_padding_mask = encoder_padding_mask.to(x.device)

        for i, len in enumerate(src_lengths):
            for j in range(len):
                j -= 1
                encoder_padding_mask[i, j] = 0

        encoder_states = [] if return_all_hiddens else None

        # encoder layers
        for layer in self.layers:
            x = layer(x, encoder_padding_mask)
            if return_all_hiddens:
                assert encoder_states is not None
                encoder_states.append(x)

        if self.layer_norm is not None:
            x = self.layer_norm(x)

        return EncoderOut(
            encoder_out=x,  # T x B x C
            encoder_padding_mask=encoder_padding_mask,  # B x T
            encoder_embedding=None,  # B x T x C
            encoder_states=encoder_states,  # List[T x B x C]
            src_tokens=None,
            src_lengths=None,
        )

    @torch.jit.export
    def reorder_encoder_out(self, encoder_out: EncoderOut, new_order):
        """
        Reorder encoder output according to *new_order*.
        Args:
            encoder_out: output from the ``forward()`` method
            new_order (LongTensor): desired order
        Returns:
            *encoder_out* rearranged according to *new_order*
        """
        """
        Since encoder_padding_mask and encoder_embedding are both of type
        Optional[Tensor] in EncoderOut, they need to be copied as local
        variables for Torchscript Optional refinement
        """
        encoder_padding_mask: Optional[
            Tensor] = encoder_out.encoder_padding_mask
        encoder_embedding: Optional[Tensor] = encoder_out.encoder_embedding

        new_encoder_out = (
            encoder_out.encoder_out
            if encoder_out.encoder_out is None
            else encoder_out.encoder_out.index_select(1, new_order)
        )
        new_encoder_padding_mask = (
            encoder_padding_mask
            if encoder_padding_mask is None
            else encoder_padding_mask.index_select(0, new_order)
        )
        new_encoder_embedding = (
            encoder_embedding
            if encoder_embedding is None
            else encoder_embedding.index_select(0, new_order)
        )
        src_tokens = encoder_out.src_tokens
        if src_tokens is not None:
            src_tokens = src_tokens.index_select(0, new_order)

        src_lengths = encoder_out.src_lengths
        if src_lengths is not None:
            src_lengths = src_lengths.index_select(0, new_order)

        encoder_states = encoder_out.encoder_states
        if encoder_states is not None:
            for idx, state in enumerate(encoder_states):
                encoder_states[idx] = state.index_select(1, new_order)

        return EncoderOut(
            encoder_out=new_encoder_out,  # T x B x C
            encoder_padding_mask=new_encoder_padding_mask,  # B x T
            encoder_embedding=new_encoder_embedding,  # B x T x C
            encoder_states=encoder_states,  # List[T x B x C]
            src_tokens=src_tokens,  # B x T
            src_lengths=src_lengths,  # B x 1
        )

    def max_positions(self):
        """Maximum input length supported by the encoder."""
        if self.embed_positions is None:
            return self.max_source_positions
        return min(self.max_source_positions,
                   self.embed_positions.max_positions)

    def upgrade_state_dict_named(self, state_dict, name):
        """Upgrade a (possibly old) state dict for new versions of fairseq."""
        if isinstance(self.embed_positions, SinusoidalPositionalEmbedding):
            weights_key = "{}.embed_positions.weights".format(name)
            if weights_key in state_dict:
                print("deleting {0}".format(weights_key))
                del state_dict[weights_key]
            state_dict[
                "{}.embed_positions._float_tensor".format(name)
            ] = torch.FloatTensor(1)
        for i in range(self.num_layers):
            # update layer norms
            self.layers[i].upgrade_state_dict_named(
                state_dict, "{}.layers.{}".format(name, i)
            )

        version_key = "{}.version".format(name)
        if utils.item(state_dict.get(version_key, torch.Tensor([1]))[0]) < 2:
            # earlier checkpoints did not normalize after the stack of layers
            self.layer_norm = None
            self.normalize = False
            state_dict[version_key] = torch.Tensor([1])
        return state_dict


class LinearPositionalEmbedding(nn.Module):

    def __init__(self, max_len=100):
        super(LinearPositionalEmbedding, self).__init__()

        self.pe = range(max_len)
        self.pe = torch.tensor(self.pe)
        self.pe = torch.unsqueeze(self.pe, dim=0)
        self.pe = torch.unsqueeze(self.pe, dim=0)
        self.pe = self.pe.float() / max_len


    def forward(self, inp, lengths):
        bs = inp.shape[0]
        pe = torch.cat(bs * [self.pe], dim=0).to(inp.device)

        out = torch.cat([pe, inp], dim=1)

        return out

class PositionalEncoding(nn.Module):

    def __init__(self, d_model, dropout=0.1, max_len=5000):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)

        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))

        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer('pe', pe)

    def forward(self, x):
        x = x + self.pe[:x.size(0), :]
        return self.dropout(x)

class SinusoidalPositionalEmbedding(nn.Module):
    def forward(self, inp, lenghts):
        batch_size, seq_len = inp.shape[0], inp.shape[1]
        positional_embeddings = torch.zeros(batch_size, seq_len)

        for i, len in enumerate(lenghts):
            for x in range(len):
                positional_embeddings[i, x] = math.cos(x / len * math.pi)

        inp.cat(positional_embeddings, dim=2)

        return inp

class ConvTransformerEncoder(nn.Module):
    def __init__(self, args, emb_dim):
        super(ConvTransformerEncoder, self).__init__()

        self.transformerEncoder = TransformerEncoder(args, emb_dim)

        self.conv1 = nn.Conv1d(12 * 2, 21 * 2, kernel_size=1)

    def forward(self, x, src_lengths):
        out = x.permute(0, 2, 3, 1)
        bs, n_keypoints, dim, len = out.shape

        out = out.view(bs, n_keypoints * dim, len)

        out = self.conv1(out)
        out = out.permute(0, 2, 1)
        out = self.transformerEncoder(out, src_lengths)

        # B x C x L -> B x L x C
        # out = out.permute(0, 2, 1)
        # out = out.view(bs, len, -1, dim)



        out = out.encoder_out
        out = out.permute(1, 0, 2)
        out = out.view(bs, len, 21, dim)

        return out

class TransformerEnc(nn.Module):

    def __init__(self, ninp, nhead, nhid, nout, nlayers, dropout=0.5):

        super(TransformerEnc, self).__init__()
        from torch.nn import TransformerEncoder, TransformerEncoderLayer
        self.model_type = 'Transformer'
        self.src_mask = None
        self.pos_encoder = PositionalEncoding(ninp, dropout, max_len=100)
        encoder_layers = TransformerEncoderLayer(nhid, nhead, nhid,
                                                 dropout)

        #self.linear_pos_enc = LinearPositionalEmbedding(max_len=100)
        self.transformer_encoder = TransformerEncoder(encoder_layers,
                                                      nlayers)
        # self.encoder = nn.Embedding(ntoken, ninp)
        self.ninp = ninp

        self.hidden2pose_projection = nn.Linear(nhid, nout)
        self.pose2hidden_projection = nn.Linear(ninp, nhid)

        #self.init_weights()

    def _generate_square_subsequent_mask(self, sz):
        mask = (torch.triu(torch.ones(sz, sz)) == 1).transpose(0, 1)
        mask = mask.float().masked_fill(mask == 0,
                                        float('-inf')).masked_fill(
            mask == 1, float(0.0))
        return mask

    def init_weights(self):
        initrange = 0.1
        # self.encoder.weight.data.uniform_(-initrange, initrange)
        self.decoder.bias.data.zero_()
        self.decoder.weight.data.uniform_(-initrange, initrange)

    def forward(self, src):
        bs, seq_len, n_in_joints, dim = src.shape
        src = src.view(bs, seq_len, n_in_joints * dim)

        #src = self.linear_pos_enc(src)

        if self.src_mask is None or self.src_mask.size(0) != len(src):
            device = src.device
            mask = self._generate_square_subsequent_mask(len(src)).to(
                device)
            self.src_mask = mask

        # src = self.encoder(src) * math.sqrt(self.ninp)
        # B x T x C -> T x B x C
        src = src.permute(1, 0, 2)
        src = self.pos_encoder(src)

        src = self.pose2hidden_projection(src)
        output = self.transformer_encoder(src)
        output = self.hidden2pose_projection(output)
        output = output.permute(1, 0, 2)

        output = output.view(bs, seq_len, -1, dim)

        return output


class TextPoseTransformer(nn.Module):
    def __init__(self, n_tokens, n_joints, joints_dim, nhead, nhid, nout, n_enc_layers, n_dec_layers, dropout=0.5):
        super(TextPoseTransformer, self).__init__()
        from torch.nn import Transformer
        self.model_type = 'Transformer'
        self.src_mask = None
        self.token_pos_encoder = PositionalEncoding(nhid, dropout,
                                              max_len=40)
        self.pose_pos_encoder = PositionalEncoding(nhid, dropout,
                                              max_len=100)

        self.transformer = Transformer(nhid, nhead, n_enc_layers, n_dec_layers, nhid, dropout=dropout)

        self.token_embedding = nn.Embedding(n_tokens, nhid)
        self.hidden2pose_projection = nn.Linear(nhid, nout)
        self.pose2hidden_projection = nn.Linear(n_joints*joints_dim, nhid)

        self.init_weights()


    def forward(self, input_tokens, input_pose):
        bs, seq_len, n_in_joints, dim = input_pose.shape
        input_pose = input_pose.view(bs, seq_len, -1)
        input_pose = input_pose.permute(1, 0, 2)

        input_tokens_embedding = self.token_embedding(input_tokens)
        input_tokens_embedding = input_tokens_embedding.permute(1, 0, 2)
        #input_pose = input_pose.reshape(bs * seq_len, -1)
        input_pose = self.pose2hidden_projection(input_pose)

        predictions = self.transformer(input_tokens_embedding, input_pose)

        predictions = self.hidden2pose_projection(predictions)

        predictions = predictions.permute(1, 0, 2)
        predictions = predictions.view(bs, seq_len, -1, dim)





        return predictions


    def init_weights(self):
        initrange = 0.1
        # self.encoder.weight.data.uniform_(-initrange, initrange)
        # self.transformer.bias.data.zero_()
        # self.transformer.weight.data.uniform_(-initrange, initrange)
        pass
