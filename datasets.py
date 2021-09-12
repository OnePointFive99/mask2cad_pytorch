import os
import json
import collections
import torch
import torch.utils.data
import torch.nn.functional as F
import torchvision
    
import pycocotools.coco
import pycocotools.mask
    

class RenderedViewsSequentialSampler(torch.utils.data.Sampler):
    def __init__(self, num_examples, num_rendered_views):
        self.num_examples = num_examples
        self.num_rendered_views = num_rendered_views
        self.idx = torch.arange(1, 1 + num_rendered_views).unsqueeze(0).expand(num_examples, -1)

    def __iter__(self):
        return iter(self.idx.tolist())

    def __len__(self):
        return len(self.idx)

class RenderedViewsRandomSampler(torch.utils.data.Sampler):
    def __init__(self, num_examples, num_rendered_views, num_sampled_views, num_sampled_boxes):
        self.num_examples = num_examples
        self.num_rendered_views = num_rendered_views
        self.num_sampled_views = num_sampled_views
        self.num_sampled_boxes = num_sampled_boxes
        self.idx = None

    def set_epoch(self, epoch):
        rng = torch.Generator()
        rng.manual_seed(epoch)

        example_idx   = torch.arange(self.num_examples, dtype = torch.int64)[:, None]
        main_view_idx = torch. zeros(self.num_examples, dtype = torch.int64)[:, None]
        novel_view_idx = 1 + torch.rand(self.num_examples * self.num_sampled_boxes, self.num_rendered_views, generator = rng).argsort(-1)[..., :self.num_sampled_views].reshape(self.num_examples, -1)

        #self.idx = torch.cat([example_idx, main_view_idx, novel_view_idx], dim = -1)
        self.idx = torch.cat([example_idx, novel_view_idx], dim = -1)
        
    def __iter__(self):
        return iter(self.idx.tolist())

    def __len__(self):
        return len(self.idx)

class UniqueShapeRenderedViewsSequentialSampler(torch.utils.data.Sampler):
    def __init__(self, dataset, num_rendered_views):
        shape2idx = {m['model'] : i for i, m in enumerate(dataset.dataset.metadata)}
        self.idx = torch.cat([torch.tensor(list(shape2idx.values()), dtype = torch.int64).unsqueeze(-1), torch.arange(1, 1 + num_rendered_views, dtype = torch.int64).repeat(len(shape2idx), 1)], dim = -1)
        
    def __iter__(self):
        return iter(self.idx.tolist())

    def __len__(self):
        return len(self.idx)

class RenderedViews(torchvision.datasets.VisionDataset):
    def __init__(self, root, clustered_rotations_path, dataset, ext = '.jpg'):
        super().__init__(root = root)
        self.dataset = dataset
        self.ext = ext
        self.clustered_rotations = torch.tensor(list(map(json.load(open(clustered_rotations_path)).get, dataset.categories)), dtype = torch.float32)

    def __getitem__(self, idx):
        img, extra = self.dataset[idx[0]]
        view_dir = os.path.join(self.root, extra['shape_path'])
        or_jpg = lambda path, ext = '.png': torchvision.io.read_image(path if os.path.exists(path) else path.replace(ext, '.jpg'))
        no_img = lambda idx: [k for k in idx if k > 0]
        fixup = lambda path: path if os.path.exists(path) else os.path.join(os.path.dirname(os.path.dirname(path)), 'model.obj', os.path.basename(path))
        
        views = torch.stack([or_jpg(os.path.join(self.root, extra['image_id']) if k == 0 else fixup(os.path.join(view_dir, f'{k:04}' + self.ext))) for k in no_img(idx[1:])])

        return img, extra, views.expand(-1, 3, -1, -1) / 255.0

    def __len__(self):
        return len(self.dataset)

class Pix3d(torchvision.datasets.VisionDataset):
    categories           = ['bed', 'bookcase', 'chair', 'desk', 'misc', 'sofa', 'table', 'tool', 'wardrobe']
    categories_coco_inds = [65   , -1        , 63      , -1   , -1    ,  63   , 67     , -1    ,  -1       ]

    def __init__(self, root, split_path = None, max_image_size = None, target_image_size = (320, 240), drop_images = ['img/table/1749.jpg', 'img/table/0045.png'], read_image = True, read_mask = True, transforms = [], **kwargs):
        super().__init__(root = root, transforms = transforms, **kwargs)
        self.target_image_size = target_image_size
        self.read_image = read_image
        self.read_mask = read_mask
        self.transforms = transforms
        metadata_full = json.load(open(os.path.join(root, 'pix3d.json')))
        
        assert set(collections.Counter(m['img'] for m in metadata_full).values()) == {1}
        
        self.shape_idx = {t        : i for i, t in enumerate(sorted(set(m['model'] for m in metadata_full)))}
        self.category_idx = {category : i for i, category in enumerate(self.categories)}

        if split_path:
            split = json.load(open(split_path))
            images = {i['id'] : dict(img = i['file_name'], img_size = [i['width'], i['height']]) for i in split['images']}
            self.metadata = [dict(bbox = a['bbox'][:2] + [a['bbox'][0] + a['bbox'][2] - 1, a['bbox'][1] + a['bbox'][3] - 1], mask = a['segmentation'], model = a['model'], rot_mat = a['rot_mat'], trans_mat = a['trans_mat'], category = self.categories[a['category_id'] - 1], focal_length = a['K'][0] * 32 / images[a['image_id']]['img_size'][0], **images[a['image_id']]) for a in split['annotations']]
        else:
            self.metadata = metadata_full

        assert all(m['bbox'][0] <= m['bbox'][2] and m['bbox'][1] <= m['bbox'][3] for m in self.metadata)

        drop_image_size = max_image_size and sum(max_image_size)
        self.metadata = [m for m in self.metadata if (m['img'] not in drop_images) and (not drop_image_size or (m['img_size'][0] <= max_image_size[0] and m['img_size'][1] <= max_image_size[1]))] 

        self.image_idx = {m['img'] : dict(m = m, file_name = m['img'], width = m['img_size'][0], height = m['img_size'][1]) for i, m in enumerate(self.metadata)}
        self.num_by_category = collections.Counter(self.category_idx[m['category']] for m in self.metadata)
        self.width_min_max  = (min(m['img_size'][0] for m in self.metadata), max(m['img_size'][0] for m in self.metadata))
        self.height_min_max = (min(m['img_size'][1] for m in self.metadata), max(m['img_size'][1] for m in self.metadata))
        self.aspect_ratios = torch.tensor([m['img_size'][0] / m['img_size'][1] for m in self.metadata], dtype = torch.float32)

    def __getitem__(self, idx):
        m = self.metadata[idx]
        img_size = m['img_size']
        bbox = m['bbox']
        
        img = torchvision.io.read_image(os.path.join(self.root, m['img'])) if self.read_image else torch.empty((0, img_size[1], img_size[0]), dtype = torch.uint8)
        mask = torchvision.io.read_image(os.path.join(self.root, m['mask'])) if self.read_mask else torch.empty((0, img_size[1], img_size[0]), dtype = torch.uint8)

        if self.target_image_size and sum(self.target_image_size):
            scale_factor = min(self.target_image_size[0] / img.shape[-1], self.target_image_size[1] / img.shape[-2])
            img = F.interpolate(img.unsqueeze(0), self.target_image_size).squeeze(0) if img.numel() > 0 else torch.empty((0, self.target_image_size[1], self.target_image_size[0]), dtype = torch.uint8)
            mask = F.interpolate(mask.unsqueeze(0), self.target_image_size).squeeze(0) if mask.numel() > 0 else torch.empty((0, self.target_image_size[1], self.target_image_size[0]), dtype = torch.uint8)
            #img = F.interpolate(img.unsqueeze(0), scale_factor = scale_factor).squeeze(0)
            #mask = F.interpolate(img.unsqueeze(0), scale_factor = scale_factor).squeeze(0)
            bbox = [bbox[0] * scale_factor, bbox[1] * scale_factor, bbox[2] * scale_factor, bbox[3] * scale_factor]
        
        bbox = torch.tensor(bbox).unsqueeze(0)
        area = (bbox[..., 2] - bbox[..., 0]) * (bbox[..., 3] - bbox[..., 1])
        iscrowd = torch.zeros(len(bbox), dtype = torch.uint8)
        labels = 1 + torch.tensor(self.category_idx[m['category']]).unsqueeze(0)
        masks = (mask == 255).unsqueeze(0)
        object_location = torch.as_tensor(m['trans_mat'], dtype = torch.float64).unsqueeze(0)
        object_rotation = torch.as_tensor(m['rot_mat'], dtype = torch.float64).unsqueeze(0)
        shape_idx = torch.tensor(self.shape_idx[m['model']]).unsqueeze(0)

        img = img / 255.0
        target = dict(
            image_id = m['img'],
            shape_path = m['model'],
            mask_path = m['mask'],
            category = m['category'], 
            
            boxes = bbox,
            area = area,
            iscrowd = iscrowd,
            labels = labels,
            masks = masks, 

            image_width_height = img_size,
            shape_idx = shape_idx,
            object_location = object_location,
            object_rotation = object_rotation
        )
        
        if self.transforms:
            img, target = self.transforms(img, target)

        return img, target

    def __len__(self):
        return len(self.metadata)

    def as_coco_dataset(self):
        # annotation IDs need to start at 1, not 0, see https://github.com/pytorch/vision/issues/1530
        coco_dataset_dict = dict(
            images = [dict(id = m['img'], height = m['img_size'][1], width = m['img_size'][0]) for m in self.metadata], 
            
            categories = [dict(id = 1 + category_idx, name = category) for category_idx, category in enumerate(self.categories)], 
            
            annotations = [dict(id = 1 + image_idx, image_id = m['img'], bbox = [m['bbox'][0], m['bbox'][1], m['bbox'][2] - m['bbox'][0], m['bbox'][3] - m['bbox'][1]], iscrowd = 0, area = (m['bbox'][2] - m['bbox'][0]) * (m['bbox'][3] - m['bbox'][1]), category_id = 1 + self.category_idx[m['category']], segmentation = pycocotools.mask.encode( torchvision.io.read_image(os.path.join(self.root, m['mask']))[0].eq(255).t().contiguous().t().numpy() )   ) for image_idx, m in enumerate(self.metadata)]
        )
        
        coco_dataset = pycocotools.coco.COCO()
        coco_dataset.dataset = coco_dataset_dict
        coco_dataset.createIndex()
        return coco_dataset
    
    def getCatIds(self):
        return list(range(len(self.categories)))
    
    def loadCats(self, ids):
        return [dict(name = self.categories[i]) for i in ids]
    
    def loadImgs(self, ids):
        return [self.image_idx[i] for i in ids]
        
    def getAnnIds(self, imgIds):
        return imgIds
    
    def loadAnns(self, ids):
        breakpoint()
        return [dict(image_id = i, segmentation = m['mask'], rot_mat = m['rot_mat'], trans_mat = m['trans_mat'], model = m['model'], category_id = self.category_idx[m['category']], bbox = m['bbox'][:2] + [m['bbox'][2] - m['bbox'][0] + 1, m['bbox'][3] - m['bbox'][1] + 1], K = [m['focal_length'] * m['img_size'][0] / 32, m['img_size'][0] / 2, m['img_size'][1] / 2]) for i in ids for m in [self.image_idx[i]['m']]]
    


def collate_fn(batch):
    assert batch

    images = torch.stack([b[0] for b in batch])
    
    targets = dict(
        image_id = [b[1]['image_id'] for b in batch], 
        shape_path = [b[1]['shape_path'] for b in batch], 
        mask_path = [b[1]['mask_path'] for b in batch], 
        category = [b[1]['category'] for b in batch],

        boxes = torch.stack([b[1]['boxes'] for b in batch]),
        masks = torch.stack([b[1]['masks'] for b in batch]), 
        shape_idx = torch.stack([b[1]['shape_idx'] for b in batch]), 
        labels = torch.stack([b[1]['labels'] for b in batch]), 
        object_location = torch.stack([b[1]['object_location'] for b in batch]),
        object_rotation = torch.stack([b[1]['object_rotation'] for b in batch]),
        views = torch.stack([b[2] for b in batch]) if len(batch[0]) > 2 else None
    )
   
    return images, targets

def _collate_fn(batch):
    return tuple(zip(*batch))

def _to_device(batch, device):
    images = [image.to(device) for image in images]
    targets = [{k: v.to(device) if torch.is_tensor(v) else v for k, v in t.items()} for t in targets]
    return images, targets
