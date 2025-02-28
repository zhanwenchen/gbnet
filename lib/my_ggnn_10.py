##################################################################
# From my_ggnn_09: Dynamically connecting entities to ontology too
# Also a minor change: img2ont edges are now normalized over ont rather than img
##################################################################
from torch import tensor as torch_tensor, float32 as torch_float32, \
    int64 as torch_int64, arange as torch_arange, mm as torch_mm, \
    zeros as torch_zeros, \
    sigmoid as torch_sigmoid, tanh as torch_tanh, cat as torch_cat, \
    sum as torch_sum, abs as torch_abs, no_grad as torch_no_grad
from torch.cuda import current_device
from torch.nn import Module, Linear, ModuleList, Sequential, ReLU, LayerNorm
from torch.nn.functional import softmax as F_softmax, relu as F_relu, \
                                normalize as F_normalize
import numpy as np
from pickle import load as pickle_load
from lib.my_util import MLP, adj_normalize
from lib.lrga import LowRankAttention


CUDA_DEVICE = current_device()


def wrap(nparr):
    return torch_tensor(nparr, dtype=torch_float32, device=CUDA_DEVICE, requires_grad=False)

def arange(num):
    return torch_arange(num, dtype=torch_int64, device=CUDA_DEVICE)

class GGNN(Module):
    def __init__(self, emb_path, graph_path, time_step_num=3, hidden_dim=512, \
                 output_dim=512, use_embedding=True, use_knowledge=True, \
                 refine_obj_cls=False, num_ents=151, num_preds=51, \
                 config=None, with_clean_classifier=None, with_transfer=None, \
                 num_obj_cls=None, num_rel_cls=None, sa=None, lrga=None):
        super(GGNN, self).__init__()
        self.time_step_num = time_step_num

        self.with_clean_classifier = with_clean_classifier
        self.with_transfer = with_transfer
        self.use_lrga = config.MODEL.LRGA.USE_LRGA
        self.k = config.MODEL.LRGA.K
        self.dropout = config.MODEL.LRGA.DROPOUT
        self.in_channels = hidden_dim
        self.hidden_channels = hidden_dim
        self.out_channels = hidden_dim

        if self.use_lrga is True:
            self.attention = ModuleList()
            self.dimension_reduce = ModuleList()
            self.attention.append(LowRankAttention(self.k, self.in_channels, self.dropout))
            self.dimension_reduce.append(Sequential(Linear(2*self.k + self.hidden_channels, self.hidden_channels, device=CUDA_DEVICE), ReLU()))
            for _ in range(self.time_step_num):
                self.attention.append(LowRankAttention(self.k, self.hidden_channels, self.dropout))
                self.dimension_reduce.append(Sequential(Linear(2*self.k + self.hidden_channels, self.hidden_channels, device=CUDA_DEVICE)))
            self.dimension_reduce[-1] = Sequential(Linear(2*self.k + self.hidden_channels, self.out_channels, device=CUDA_DEVICE))
            # self.gn = ModuleList([GroupNorm(self.num_groups, self.hidden_channels) for _ in range(self.time_step_num-1)])
            self.gn = ModuleList([LayerNorm(self.hidden_channels) for _ in range(self.time_step_num-1)])

        if use_embedding:
            with open(emb_path, 'rb') as fin:
                self.emb_ent, self.emb_pred = pickle_load(fin)
            self.emb_ent = wrap(self.emb_ent)
            self.emb_pred = wrap(self.emb_pred)
        else:
            self.emb_ent = torch_eye(num_ents, dtype=torch_float32)
            self.emb_pred = torch_eye(num_preds, dtype=torch_float32)

        self.num_ont_ent = self.emb_ent.size(0)
        assert self.num_ont_ent == num_obj_cls
        self.num_ont_pred = self.emb_pred.size(0)
        assert self.num_ont_pred == num_rel_cls

        if use_knowledge:
            with open(graph_path, 'rb') as fin:
                edge_dict = pickle_load(fin)
            self.adjmtx_ent2ent = edge_dict['edges_ent2ent']
            self.adjmtx_ent2pred = edge_dict['edges_ent2pred']
            self.adjmtx_pred2ent = edge_dict['edges_pred2ent']
            self.adjmtx_pred2pred = edge_dict['edges_pred2pred']
        else:
            self.adjmtx_ent2ent = np.zeros((1, num_ents, num_ents), dtype=np.float32)
            self.adjmtx_ent2pred = np.zeros((1, num_ents, num_preds), dtype=np.float32)
            self.adjmtx_pred2ent = np.zeros((1, num_preds, num_ents), dtype=np.float32)
            self.adjmtx_pred2pred = np.zeros((1, num_preds, num_preds), dtype=np.float32)

        self.edges_ont_ent2ent = wrap(self.adjmtx_ent2ent)
        self.edges_ont_ent2pred = wrap(self.adjmtx_ent2pred)
        self.edges_ont_pred2ent = wrap(self.adjmtx_pred2ent)
        self.edges_ont_pred2pred = wrap(self.adjmtx_pred2pred)

        self.num_edge_types_ent2ent = self.adjmtx_ent2ent.shape[0]
        self.num_edge_types_ent2pred = self.adjmtx_ent2pred.shape[0]
        self.num_edge_types_pred2ent = self.adjmtx_pred2ent.shape[0]
        self.num_edge_types_pred2pred = self.adjmtx_pred2pred.shape[0]

        self.fc_init_ont_ent = Linear(self.emb_ent.size(1), hidden_dim)
        self.fc_init_ont_pred = Linear(self.emb_pred.size(1), hidden_dim)

        self.fc_mp_send_ont_ent = MLP([hidden_dim, hidden_dim // 2, hidden_dim // 4], act_fn='ReLU', last_act=True)
        self.fc_mp_send_ont_pred = MLP([hidden_dim, hidden_dim // 2, hidden_dim // 4], act_fn='ReLU', last_act=True)
        self.fc_mp_send_img_ent = MLP([hidden_dim, hidden_dim // 2, hidden_dim // 4], act_fn='ReLU', last_act=True)
        self.fc_mp_send_img_pred = MLP([hidden_dim, hidden_dim // 2, hidden_dim // 4], act_fn='ReLU', last_act=True)

        self.fc_mp_receive_ont_ent = MLP([(self.num_edge_types_ent2ent + self.num_edge_types_pred2ent + 1) * hidden_dim // 4,
                                          (self.num_edge_types_ent2ent + self.num_edge_types_pred2ent + 1) * hidden_dim // 4,
                                          hidden_dim], act_fn='ReLU', last_act=True)
        self.fc_mp_receive_ont_pred = MLP([(self.num_edge_types_ent2pred + self.num_edge_types_pred2pred + 1) * hidden_dim // 4,
                                           (self.num_edge_types_ent2pred + self.num_edge_types_pred2pred + 1) * hidden_dim // 4,
                                           hidden_dim], act_fn='ReLU', last_act=True)
        self.fc_mp_receive_img_ent = MLP([3 * hidden_dim // 4, 3 * hidden_dim // 4, hidden_dim], act_fn='ReLU', last_act=True)
        self.fc_mp_receive_img_pred = MLP([3 * hidden_dim // 4, 3 * hidden_dim // 4, hidden_dim], act_fn='ReLU', last_act=True)

        self.fc_eq3_w_ont_ent = Linear(hidden_dim, hidden_dim)
        self.fc_eq3_u_ont_ent = Linear(hidden_dim, hidden_dim)
        self.fc_eq4_w_ont_ent = Linear(hidden_dim, hidden_dim)
        self.fc_eq4_u_ont_ent = Linear(hidden_dim, hidden_dim)
        self.fc_eq5_w_ont_ent = Linear(hidden_dim, hidden_dim)
        self.fc_eq5_u_ont_ent = Linear(hidden_dim, hidden_dim)

        self.fc_eq3_w_ont_pred = Linear(hidden_dim, hidden_dim)
        self.fc_eq3_u_ont_pred = Linear(hidden_dim, hidden_dim)
        self.fc_eq4_w_ont_pred = Linear(hidden_dim, hidden_dim)
        self.fc_eq4_u_ont_pred = Linear(hidden_dim, hidden_dim)
        self.fc_eq5_w_ont_pred = Linear(hidden_dim, hidden_dim)
        self.fc_eq5_u_ont_pred = Linear(hidden_dim, hidden_dim)

        self.fc_eq3_w_img_ent = Linear(hidden_dim, hidden_dim)
        self.fc_eq3_u_img_ent = Linear(hidden_dim, hidden_dim)
        self.fc_eq4_w_img_ent = Linear(hidden_dim, hidden_dim)
        self.fc_eq4_u_img_ent = Linear(hidden_dim, hidden_dim)
        self.fc_eq5_w_img_ent = Linear(hidden_dim, hidden_dim)
        self.fc_eq5_u_img_ent = Linear(hidden_dim, hidden_dim)

        self.fc_eq3_w_img_pred = Linear(hidden_dim, hidden_dim)
        self.fc_eq3_u_img_pred = Linear(hidden_dim, hidden_dim)
        self.fc_eq4_w_img_pred = Linear(hidden_dim, hidden_dim)
        self.fc_eq4_u_img_pred = Linear(hidden_dim, hidden_dim)
        self.fc_eq5_w_img_pred = Linear(hidden_dim, hidden_dim)
        self.fc_eq5_u_img_pred = Linear(hidden_dim, hidden_dim)

        self.fc_output_proj_img_pred = MLP([hidden_dim, hidden_dim, hidden_dim], act_fn='ReLU', last_act=False)
        self.fc_output_proj_ont_pred = MLP([hidden_dim, hidden_dim, hidden_dim], act_fn='ReLU', last_act=False)

        self.refine_obj_cls = refine_obj_cls
        if self.refine_obj_cls:
            self.fc_output_proj_img_ent = MLP([hidden_dim, hidden_dim, hidden_dim], act_fn='ReLU', last_act=False)
            self.fc_output_proj_ont_ent = MLP([hidden_dim, hidden_dim, hidden_dim], act_fn='ReLU', last_act=False)

        self.debug_info = {}

        self.with_clean_classifier = with_clean_classifier
        self.with_transfer = with_transfer
        self.sa = sa
        self.use_ontological_adjustment = config.MODEL.USE_ONTOLOGICAL_ADJUSTMENT
        self.normalize_eoa = config.MODEL.NORMALIZE_EOA
        self.shift_eoa = config.MODEL.SHIFT_EOA
        self.fold_eoa = config.MODEL.FOLD_EOA
        self.merge_eoa_sa = config.MODEL.MERGE_EOA_SA

        if self.use_ontological_adjustment is True:
            print('my_ggnn_10: using use_ontological_adjustment')
            ontological_preds = self.adjmtx_pred2pred[3, :, :]
            if self.fold_eoa is True:
                diag_indices = np.diag_indices(ontological_preds.shape[0])
                folded = ontological_preds + ontological_preds.T
                folded[diag_indices] = ontological_preds[diag_indices]
            if self.shift_eoa is True:
                ontological_preds += 1.0
                print(f'EOA-N: Used shift_eoa')
            else:
                print(f'EOA-N: Not using shift_eoa. self.eoa_n={self.normalize_eoa}')
            if not self.normalize_eoa:
                ontological_preds = ontological_preds / (ontological_preds.sum(-1)[:, None] + 1e-8)
                print(f'EOA-N: Not using normalize_eoa. Using BPL\'s original normalization')
            self.ontological_preds = torch_tensor(ontological_preds, dtype=torch_float32, device=CUDA_DEVICE)
            if self.normalize_eoa is True:
                F_normalize(self.ontological_preds, out=self.ontological_preds)
                print(f'EOA-N: Used normalize_eoa')
        else:
            print(f'my_ggnn_10: not using use_ontological_adjustment. self.use_ontological_adjustment={self.use_ontological_adjustment}')

        if self.with_clean_classifier:
            self.fc_output_proj_img_pred_clean = MLP([hidden_dim, hidden_dim, hidden_dim], act_fn='ReLU', last_act=False)
            self.fc_output_proj_ont_pred_clean = MLP([hidden_dim, hidden_dim, hidden_dim], act_fn='ReLU', last_act=False)

            if self.refine_obj_cls:
                self.fc_output_proj_img_ent_clean = MLP([hidden_dim, hidden_dim, hidden_dim], act_fn='ReLU', last_act=False)
                self.fc_output_proj_ont_ent_clean = MLP([hidden_dim, hidden_dim, hidden_dim], act_fn='ReLU', last_act=False)

            if self.with_transfer is True:
                print("!!!!!!!!!With Confusion Matrix Channel!!!!!")
                pred_adj_np = np.load(config.MODEL.CONF_MAT_FREQ_TRAIN)
                # pred_adj_np = 1.0 - pred_adj_np
                pred_adj_np[0, :] = 0.0
                pred_adj_np[:, 0] = 0.0
                pred_adj_np[0, 0] = 1.0
                # adj_i_j means the baseline outputs category j, but the ground truth is i.
                pred_adj_np = pred_adj_np / (pred_adj_np.sum(-1)[:, None] + 1e-8)
                if self.sa is True:
                    pred_adj_np = adj_normalize(pred_adj_np)
                    print(f'SA: Used adj_normalize')
                else:
                    print(f'No SA: Not using adj_normalize.self.sa={self.sa}')
                self.pred_adj_nor = torch_tensor(pred_adj_np, dtype=torch_float32, device=CUDA_DEVICE)


    def forward(self, rel_inds, obj_probs, obj_fmaps, vr):
        # This is a per_image representation, not an embedding.
        num_img_ent = obj_probs.size(0)
        num_img_pred = rel_inds.size(0)

        debug_info = self.debug_info
        debug_info['rel_inds'] = rel_inds
        debug_info['obj_probs'] = obj_probs

        refine_obj_cls = self.refine_obj_cls

        nodes_ont_ent = self.fc_init_ont_ent(self.emb_ent)
        nodes_ont_pred = self.fc_init_ont_pred(self.emb_pred)
        nodes_img_ent = obj_fmaps
        if self.use_lrga is True:
            original_vr = vr.clone()
        nodes_img_pred = vr

        edges_img_pred2subj = torch_zeros((num_img_pred, num_img_ent), dtype=torch_float32, device=CUDA_DEVICE, requires_grad=False)
        edges_img_pred2subj[arange(num_img_pred), rel_inds[:, 0]] = 1
        edges_img_pred2obj = torch_zeros((num_img_pred, num_img_ent), dtype=torch_float32, device=CUDA_DEVICE, requires_grad=False)
        edges_img_pred2obj[arange(num_img_pred), rel_inds[:, 1]] = 1
        edges_img_subj2pred = edges_img_pred2subj.t()
        edges_img_obj2pred = edges_img_pred2obj.t()

        edges_img2ont_ent = obj_probs.clone().detach()
        edges_ont2img_ent = edges_img2ont_ent.t()

        edges_img2ont_pred = torch_zeros((num_img_pred, self.num_ont_pred), dtype=torch_float32, device=CUDA_DEVICE, requires_grad=False)
        edges_ont2img_pred = edges_img2ont_pred.t()

        ent_cls_logits = None

        edges_ont_ent2ent = self.edges_ont_ent2ent
        edges_ont_pred2ent = self.edges_ont_pred2ent
        edges_ont_ent2pred = self.edges_ont_ent2pred
        edges_ont_pred2pred = self.edges_ont_pred2pred

        num_edge_types_ent2ent = self.num_edge_types_ent2ent
        num_edge_types_pred2ent = self.num_edge_types_pred2ent
        num_edge_types_ent2pred = self.num_edge_types_ent2pred
        num_edge_types_pred2pred = self.num_edge_types_pred2pred

        with_clean_classifier = self.with_clean_classifier
        with_transfer = self.with_transfer

        if with_clean_classifier and with_transfer:
            pred_adj_nor = self.pred_adj_nor

        for t in range(self.time_step_num):
            message_send_ont_ent = self.fc_mp_send_ont_ent(nodes_ont_ent)
            message_send_ont_pred = self.fc_mp_send_ont_pred(nodes_ont_pred)
            message_send_img_ent = self.fc_mp_send_img_ent(nodes_img_ent)
            message_send_img_pred = self.fc_mp_send_img_pred(nodes_img_pred)

            # NOTE: there's some vectorization opportunity right here.
            message_received_ont_ent = self.fc_mp_receive_ont_ent(torch_cat(
                [torch_mm(edges_ont_ent2ent[i].t(), message_send_ont_ent) for i in range(num_edge_types_ent2ent)] +
                [torch_mm(edges_ont_pred2ent[i].t(), message_send_ont_pred) for i in range(num_edge_types_pred2ent)] +
                [torch_mm(edges_ont2img_ent, message_send_img_ent),]
            , 1))

            message_received_ont_pred = self.fc_mp_receive_ont_pred(torch_cat(
                [torch_mm(edges_ont_ent2pred[i].t(), message_send_ont_ent) for i in range(num_edge_types_ent2pred)] +
                [torch_mm(edges_ont_pred2pred[i].t(), message_send_ont_pred) for i in range(num_edge_types_pred2pred)] +
                [torch_mm(edges_ont2img_pred, message_send_img_pred),]
            , 1))

            message_received_img_ent = self.fc_mp_receive_img_ent(torch_cat([
                torch_mm(edges_img_subj2pred, message_send_img_pred),
                torch_mm(edges_img_obj2pred, message_send_img_pred),
                torch_mm(edges_img2ont_ent, message_send_ont_ent),
            ], 1))

            del message_send_ont_ent, message_send_img_pred

            message_received_img_pred = self.fc_mp_receive_img_pred(torch_cat([
                torch_mm(edges_img_pred2subj, message_send_img_ent),
                torch_mm(edges_img_pred2obj, message_send_img_ent),
                torch_mm(edges_img2ont_pred, message_send_ont_pred),
            ], 1))

            del message_send_ont_pred, message_send_img_ent

            z_ont_ent = torch_sigmoid(self.fc_eq3_w_ont_ent(message_received_ont_ent) + self.fc_eq3_u_ont_ent(nodes_ont_ent))
            r_ont_ent = torch_sigmoid(self.fc_eq4_w_ont_ent(message_received_ont_ent) + self.fc_eq4_u_ont_ent(nodes_ont_ent))
            h_ont_ent = torch_tanh(self.fc_eq5_w_ont_ent(message_received_ont_ent) + self.fc_eq5_u_ont_ent(r_ont_ent * nodes_ont_ent))
            del message_received_ont_ent, r_ont_ent
            # nodes_ont_ent_new = (1 - z_ont_ent) * nodes_ont_ent + z_ont_ent * h_ont_ent
            nodes_ont_ent = (1 - z_ont_ent) * nodes_ont_ent + z_ont_ent * h_ont_ent
            del z_ont_ent, h_ont_ent

            z_ont_pred = torch_sigmoid(self.fc_eq3_w_ont_pred(message_received_ont_pred) + self.fc_eq3_u_ont_pred(nodes_ont_pred))
            r_ont_pred = torch_sigmoid(self.fc_eq4_w_ont_pred(message_received_ont_pred) + self.fc_eq4_u_ont_pred(nodes_ont_pred))
            h_ont_pred = torch_tanh(self.fc_eq5_w_ont_pred(message_received_ont_pred) + self.fc_eq5_u_ont_pred(r_ont_pred * nodes_ont_pred))
            del message_received_ont_pred, r_ont_pred
            nodes_ont_pred = (1 - z_ont_pred) * nodes_ont_pred + z_ont_pred * h_ont_pred
            del z_ont_pred, h_ont_pred

            z_img_ent = torch_sigmoid(self.fc_eq3_w_img_ent(message_received_img_ent) + self.fc_eq3_u_img_ent(nodes_img_ent))
            r_img_ent = torch_sigmoid(self.fc_eq4_w_img_ent(message_received_img_ent) + self.fc_eq4_u_img_ent(nodes_img_ent))
            h_img_ent = torch_tanh(self.fc_eq5_w_img_ent(message_received_img_ent) + self.fc_eq5_u_img_ent(r_img_ent * nodes_img_ent))
            del message_received_img_ent, r_img_ent
            nodes_img_ent = (1 - z_img_ent) * nodes_img_ent + z_img_ent * h_img_ent
            del z_img_ent, h_img_ent

            z_img_pred = torch_sigmoid(self.fc_eq3_w_img_pred(message_received_img_pred) + self.fc_eq3_u_img_pred(nodes_img_pred))
            r_img_pred = torch_sigmoid(self.fc_eq4_w_img_pred(message_received_img_pred) + self.fc_eq4_u_img_pred(nodes_img_pred))
            h_img_pred = torch_tanh(self.fc_eq5_w_img_pred(message_received_img_pred) + self.fc_eq5_u_img_pred(r_img_pred * nodes_img_pred))
            del message_received_img_pred, r_img_pred
            nodes_img_pred = (1 - z_img_pred) * nodes_img_pred + z_img_pred * h_img_pred
            del z_img_pred, h_img_pred
            if self.use_lrga is True:
                nodes_img_pred = self.dimension_reduce[t](torch_cat((self.attention[t](original_vr), nodes_img_pred), dim=1))
                if t != self.time_step_num - 1:
                    # No ReLU nor batchnorm for last layer
                    nodes_img_pred = self.gn[t](F_relu(nodes_img_pred))

            if not with_clean_classifier:
                pred_cls_logits = torch_mm(self.fc_output_proj_img_pred(nodes_img_pred), self.fc_output_proj_ont_pred(nodes_ont_pred).t())

            if with_clean_classifier:
                pred_cls_logits = torch_mm(self.fc_output_proj_img_pred_clean(nodes_img_pred), self.fc_output_proj_ont_pred_clean(nodes_ont_pred).t())
                if with_transfer is True and self.merge_eoa_sa is True:
                    pred_cls_logits = (F_normalize(pred_adj_nor + self.ontological_preds, p=1) @ pred_cls_logits.T).T
                else:
                    pred_cls_logits = (pred_adj_nor @ pred_cls_logits.T).T

            if self.use_ontological_adjustment is True and not self.merge_eoa_sa:
                pred_cls_logits = (self.ontological_preds @ pred_cls_logits.T).T

            edges_img2ont_pred = F_softmax(pred_cls_logits, dim=1)
            edges_ont2img_pred = edges_img2ont_pred.t()
            if refine_obj_cls:
                ent_cls_logits = torch_mm(self.fc_output_proj_img_ent(nodes_img_ent), self.fc_output_proj_ont_ent(nodes_ont_ent).t())
                edges_img2ont_ent = F_softmax(ent_cls_logits, dim=1)
                edges_ont2img_ent = edges_img2ont_ent.t()

        return pred_cls_logits, ent_cls_logits
