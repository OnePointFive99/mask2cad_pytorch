import math

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision

import quat

class ShapeRetrieval(nn.Module):
    def __init__(self, model, rendered_views_dataset):
        super().__init__()
        self.model = model
        self.shape_embedding = []
        self.shape_indices = []
        for img, extra, views in rendered_views_dataset:
            self.shape_embedding.extend(model.rendered_view_encoder(views))
            self.shape_indices.extend(extra['shape_idx'])
        self.shape_embedding = F.normalize(torch.cat(self.shape_embedding), dim = -1)
        self.shape_idx = torch.cat(self.shape_idx)
        
    def forward(self, *args, **kwargs):
        detections = self.model(*args, **kwargs)
        num_boxes = [len(d['boxes']) for d in detections]
        shape_embedding = F.normalize(torch.cat([d['shape_embedding'] for d in detections]), dim = -1)
        shape_idx = (shape_embedding @ self.shape_embedding.transpose(-2, -1)).argmax(dim = -1)

        for d, idx in zip(detections, shape_idx.split(num_boxes)):
            d['shape_idx'] = idx

        return detections

class Mask2CAD(nn.Module):
    def __init__(self, num_categories = 9, embedding_dim = 256, num_rotation_clusters = 16, shape_embedding_dim = 128, detections_per_image = 8, object_rotation_quat = None):
        super().__init__()
        # TODO: buffer?
        self.object_rotation_quat = object_rotation_quat
        self.num_rotation_clusters = num_rotation_clusters
        self.num_categories_with_bg = num_categories
        
        self.rendered_view_encoder = torchvision.models.resnet18(pretrained = False)
        self.rendered_view_encoder.fc = nn.Linear(self.rendered_view_encoder.fc.in_features, shape_embedding_dim)
        
        self.object_detector = torchvision.models.detection.maskrcnn_resnet50_fpn(pretrained = True, trainable_backbone_layers = 0)
        self.object_detector.roi_heads.box_predictor = torchvision.models.detection.faster_rcnn.FastRCNNPredictor(self.object_detector.roi_heads.box_predictor.cls_score.in_features, num_categories)
        self.object_detector.roi_heads.mask_predictor = torchvision.models.detection.mask_rcnn.MaskRCNNPredictor(self.object_detector.roi_heads.mask_predictor.conv5_mask.in_channels, 256, num_categories)
        self.object_detector.roi_heads.mask_predictor = CacheInputOutput(self.object_detector.roi_heads.mask_predictor)
        self.object_detector.roi_heads.mask_roi_pool = CacheInputOutput(self.object_detector.roi_heads.mask_roi_pool)
        
        conv_bn_relu = lambda in_channels = embedding_dim, out_channels = embedding_dim, kernel_size = 3: nn.Sequential(nn.Conv2d(in_channels, out_channels, kernel_size = kernel_size, padding = kernel_size // 2), nn.BatchNorm2d(out_channels), nn.ReLU(True))
        
        self.shape_embedding_branch = nn.Sequential(*([conv_bn_relu() for k in range(3)] + [conv_bn_relu(embedding_dim, shape_embedding_dim), nn.AdaptiveAvgPool2d(1), nn.Flatten(start_dim = -3)]))
        self.pose_classification_branch = nn.Sequential(*([conv_bn_relu() for k in range(4)] + [nn.AdaptiveAvgPool2d(1), nn.Flatten(start_dim = -3), nn.Linear(embedding_dim, self.num_categories_with_bg * self.num_rotation_clusters)]))
        self.pose_refinement_branch = nn.Sequential(*([conv_bn_relu() for k in range(4)] + [nn.AdaptiveAvgPool2d(1), nn.Flatten(start_dim = -3), nn.Linear(embedding_dim, self.num_categories_with_bg * 4)]))
        self.center_regression_branch = nn.Sequential(*([conv_bn_relu() for k in range(4)] + [nn.AdaptiveAvgPool2d(1), nn.Flatten(start_dim = -3), nn.Linear(embedding_dim, self.num_categories_with_bg * 2)]))

        self.reset_parameters()

    @torch.no_grad()
    def reset_parameters(self, quat_fill = 0.95):
        self.pose_refinement_branch[-1].bias.zero_()
        self.pose_refinement_branch[-1].bias[3::4] = quat_fill # xyzw

    def forward(self, img : 'B3HW', *, rendered : 'BQV3HW', category_idx : 'BQ' = None, shape_idx : 'BQ' = None, bbox : 'BQ4' = None, object_location : 'BQ3' = None, object_rotation_quat : 'BQ4' = None, loss_weights = dict(shape_embedding = 0.5, pose_classification = 0.25, pose_regression = 5.0), P = 4, N = 8):
        B = img.shape[0]
        
        if self.training:
            images = self.object_detector.transform(img)[0]
            img_features = self.object_detector.backbone(images.tensors)
            box_features = self.object_detector.roi_heads.box_roi_pool(img_features, bbox.unbind(), images.image_sizes)
        else:
            detections = self.object_detector(img)
            num_boxes = [len(d['boxes']) for d in detections]
            box_features = self.object_detector.roi_heads.box_roi_pool(*self.object_detector.roi_heads.mask_roi_pool.args)
            mask_logits = self.object_detector.roi_heads.mask_predictor.output
            category_idx = torch.cat([d['labels'] for d in detections])
            mask_probs = self.gather_incomplete(mask_logits, category_idx).sigmoid()
            box_features = F.interpolate(box_features, mask_probs.shape[-2:]) * mask_probs.unsqueeze(-3)
            bbox = torch.cat([d['boxes'] for d in detections])
        
        Q = bbox.shape[1]
        shape_embedding = self.shape_embedding_branch(box_features).unflatten(0, (B, Q))
        object_rotation_bins = self.pose_classification_branch(box_features).unflatten(0, (B, Q)).unflatten(-1, (self.num_categories_with_bg, self.num_rotation_clusters))
        object_rotation_delta = self.pose_refinement_branch(box_features).unflatten(0, (B, Q)).unflatten(-1, (self.num_categories_with_bg, 4))
        center_delta = self.center_regression_branch(box_features).unflatten(0, (B, Q)).unflatten(-1, (self.num_categories_with_bg, 2))
        object_rotation_bins, object_rotation_delta, center_delta = [self.gather_incomplete(t, category_idx) for t in [object_rotation_bins, object_rotation_delta, center_delta]]

        if self.training:
            V = rendered.shape[-4] // Q
            rendered_view_features = self.rendered_view_encoder(rendered.flatten(end_dim = -4)).unflatten(0, (B, Q, V))
        
            target_object_rotation_bins, target_object_rotation_delta, target_center_delta = self.compute_rotation_location_targets(bbox, object_location, object_rotation_quat, category_idx)
            shape_embedding_loss = self.shape_embedding_loss(shape_embedding, rendered_view_features, category_idx = category_idx, shape_idx = shape_idx, P = P, N = N)
            pose_classification_loss, pose_regression_loss, center_regression_loss = self.pose_estimation_loss(object_rotation_bins, object_rotation_delta, center_delta, target_object_rotation_bins, target_object_rotation_delta, target_center_delta)
            
            loss = loss_weights['shape_embedding'] * shape_embedding_loss + loss_weights['pose_classification'] * pose_classification_loss + loss_weights['pose_regression'] * pose_regression_loss + loss_weights['pose_regression'] * center_regression_loss
            
            return loss

        else:
            anchor_quat = self.gather_incomplete(self.object_rotation_quat[category_idx], object_rotation_bins.argmax(dim = -1))
            object_rotation = quatprod(anchor_quat, object_rotation_delta)
            center_x, center_y, width, height = self.xyxy_to_cxcywh(bbox).unbind(dim = -1)
            object_location = torch.stack([center_x + center_delta[..., 0] * width, center_y + center_delta[..., 1] * height, object_location[..., 2]], dim = -1)

            for d, l, r, s in zip(detections, object_location.split(num_boxes), object_rotation.split(num_boxes), shape_embedding.split(num_boxes)):
                d['location3d'] = tuple(l.tolist())
                d['rotation3d_quat'] = tuple(r.tolist())
                d['shape_embedding'] = s

            return detections
    
    def compute_rotation_location_targets(self, bbox : 'BQ4', object_location : 'BQ3', object_rotation_quat : 'BQ4', category_idx : 'BQ'):
        anchor_quat = self.object_rotation_quat[category_idx]
        object_rotation_bins = quat.quatcdist(object_rotation_quat.unsqueeze(-2), anchor_quat).squeeze(-2).argmin(dim = -1)
        anchor_quat = self.gather_incomplete(anchor_quat, object_rotation_bins)
        
        # x * q = t
        # x = t * q ** -1
        object_rotation_delta = quat.quatprodinv(anchor_quat, object_rotation_quat)

        center_x, center_y, width, height = self.xyxy_to_cxcywh(bbox).unbind(dim = -1)
        center_delta = torch.stack([(object_location[..., 0] - center_x) / width, (object_location[..., 1] - center_y) / height], dim = -1)

        return object_rotation_bins, object_rotation_delta, center_delta

    @staticmethod
    def pose_estimation_loss(pred_object_rotation_bins, pred_object_rotation_delta, pred_center_delta, true_object_rotation_bins, true_object_rotation_delta, true_center_delta, delta = 0.15, theta = math.pi / 6):
        
        pose_classification_loss = F.cross_entropy(pred_object_rotation_bins.flatten(end_dim = -2), true_object_rotation_bins.flatten())
        
        pose_regression_loss = F.huber_loss(pred_object_rotation_delta, true_object_rotation_delta, delta = delta)
        
        center_regression_loss = F.huber_loss(pred_center_delta, true_center_delta, delta = delta)

        return pose_classification_loss, pose_regression_loss, center_regression_loss

    @staticmethod
    def shape_embedding_loss(img_region_features : 'BQC', rendered_view_features : 'BQVC', category_idx : 'BQ', shape_idx : 'BQ', C = 1.5, tau = 0.15, P = 32, N = 128):
        img_region_features, rendered_view_features = F.normalize(img_region_features, dim = -1), F.normalize(rendered_view_features, dim = -1)
        D = torch.mm(img_region_features.flatten(end_dim = -2), rendered_view_features.flatten(end_dim = -2).t()) / tau
        
        same_shape = shape_idx.reshape(-1, 1) == shape_idx.unsqueeze(-1).expand(-1, -1, rendered_view_features.shape[-2]).reshape(1, -1)
        same_category = category_idx.reshape(-1, 1) == category_idx.unsqueeze(-1).expand(-1, -1, rendered_view_features.shape[-2]).reshape(1, -1)

        Dpos = torch.where(same_shape, D, torch.full_like(D, float('inf'))).topk(P, dim = -1, largest = False).values
        Dneg = torch.where(same_category, D, torch.full_like(D, float('-inf'))).topk(N, dim = -1, largest = True).values

        loss = -(Dpos / (Dpos + C * Dneg.sum(dim = -1, keepdim = True))).log().sum(dim = -1)

        return loss.mean()

    @staticmethod
    def xyxy_to_cxcywh(bbox):
        width, height = (bbox[..., 2] - bbox[..., 0]), (bbox[..., 3] - bbox[..., 1])
        center_x, center_y = (bbox[..., 0] + width / 2), (bbox[..., 1] + height / 2)
        return torch.stack([center_x, center_y, width, height], dim = -1)

    @staticmethod
    def gather_incomplete(tensor, I):
        return tensor.gather(I.ndim, I[(...,) + (None,) * (tensor.ndim - I.ndim)].expand((-1,) * (I.ndim + 1) + tensor.shape[I.ndim + 1:])).squeeze(I.ndim)

class CacheInputOutput(nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model
        self.output = None
        self.args = ()
        self.kwargs = {}

    def forward(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.output = self.model(*args, **kwargs)
        return self.output
