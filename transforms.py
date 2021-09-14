import torch
import torchvision

import torch.nn as nn
import torchvision.transforms.functional as Fv
import torchvision.transforms.transforms as T

def MaskRCNNAugmentations(hflip_prob = 0.5)
    # TODO: add rescale?
    return RandomHorizontalFlip(p = hflip_prob)

class Mask2CADAugmentations(nn.Module):
    def __init__(self, shape_view_side_size = 128):
        super().__init__()
        self.shape_view_transforms = [RandomPhotometricDistort(), T.RandomCrop(shape_view_side_size)]

    def forward(self, image, target):
        for t in self.shape_view_transforms:
            target['shape_views'] = t(target['shape_views'])
        return image, target

class RandomHorizontalFlip(T.RandomHorizontalFlip):
    def forward(self, image, target):
        if torch.rand(1) < self.p:
            image = Fv.hflip(image)
            if target is not None:
                width, _ = Fv.get_image_size(image)
                target["boxes"][:, [0, 2]] = width - target["boxes"][:, [2, 0]]
                if "masks" in target:
                    target["masks"] = target["masks"].flip(-1)
        return image, target

class RandomPhotometricDistort(nn.Module):
    def __init__(self, contrast = (0.5, 1.5), saturation = (0.5, 1.5),
                 hue = (-0.05, 0.05), brightness = (0.875, 1.125), p = 0.5):
        super().__init__()
        self._brightness = T.ColorJitter(brightness=brightness)
        self._contrast = T.ColorJitter(contrast=contrast)
        self._hue = T.ColorJitter(hue=hue)
        self._saturation = T.ColorJitter(saturation=saturation)
        self.p = p

    def forward(self, image, target = None):
        if isinstance(image, torch.Tensor):
            if image.ndimension() not in {2, 3}:
                raise ValueError('image should be 2/3 dimensional. Got {} dimensions.'.format(image.ndimension()))
            elif image.ndimension() == 2:
                image = image.unsqueeze(0)

        r = torch.rand(7)

        if r[0] < self.p:
            image = self._brightness(image)

        contrast_before = r[1] < 0.5
        if contrast_before:
            if r[2] < self.p:
                image = self._contrast(image)

        if r[3] < self.p:
            image = self._saturation(image)

        if r[4] < self.p:
            image = self._hue(image)

        if not contrast_before:
            if r[5] < self.p:
                image = self._contrast(image)

        if r[6] < self.p:
            channels = Fv.get_image_num_channels(image)
            permutation = torch.randperm(channels)

            image = image[..., permutation, :, :]

        return image, target
