import argparse
import numpy as np
import os
import torch
from collections import namedtuple
from tqdm import tqdm

from dataset import Waymo, get_dataloader
from model import PointPillars
import deeplabv3plus.network as network
from painting.painting import Painter
from utils import setup_seed, keep_bbox_from_image_range, \
    keep_bbox_from_lidar_range, write_pickle, write_label, \
    iou2d, iou3d_camera, iou_bev
from evaluate import do_eval

def main(args):
    val_dataset = Waymo(data_root=args.data_root,
                        split='val', painted=args.painted, cam_sync=args.cam_sync, inference=True)
    val_dataloader = get_dataloader(dataset=val_dataset, 
                                    batch_size=1, 
                                    num_workers=args.num_workers,
                                    rank=0,
                                    world_size=1,
                                    shuffle=False)
    CLASSES = Waymo.CLASSES
    LABEL2CLASSES = {v:k for k, v in CLASSES.items()}

    if not args.no_cuda:
        model = PointPillars(nclasses=args.nclasses, painted=args.painted).cuda()
        checkpoint = torch.load(args.lidar_detector)
        model.load_state_dict(checkpoint["model_state_dict"])
    else:
        model = PointPillars(nclasses=args.nclasses, painted=args.painted)
        checkpoint = torch.load(args.lidar_detector, map_location=torch.device('cpu'))
        model.load_state_dict(checkpoint["model_state_dict"])
    PaintArgs = namedtuple('PaintArgs', ['training_path', 'model_path'])
    painting_args = PaintArgs(os.path.join(args.data_root, 'training'), args.segmentor)
    painter = Painter(painting_args)
    deeplab = painter.model
    saved_path = args.saved_path
    os.makedirs(saved_path, exist_ok=True)
    saved_submit_path = os.path.join(saved_path, 'submit')
    os.makedirs(saved_submit_path, exist_ok=True)

    pcd_limit_range = np.array([-74.88, -74.88, -2, 74.88, 74.88, 4], dtype=np.float32)

    model.eval()
    with torch.no_grad():
        format_results = {}
        print('Predicting and Formatting the results.')
        for i, data_dict in enumerate(tqdm(val_dataloader)):
            if not args.no_cuda:
                # move the tensors to the cuda
                for key in data_dict:
                    for j, item in enumerate(data_dict[key]):
                        if torch.is_tensor(item):
                            data_dict[key][j] = data_dict[key][j].cuda()
            
            batched_pts = data_dict['batched_pts']
            batched_gt_bboxes = data_dict['batched_gt_bboxes']
            batched_labels = data_dict['batched_labels']
            batched_images = data_dict['batched_img_info'][0]['images']
            scores_from_cam = []
            for i in range(len(batched_images)):
                segmentation_score = deeplab(batched_images[i])[0]
                scores_from_cam.append(painter.get_score(segmentation_score))

            points = painter.augment_lidar_class_scores_both(scores_from_cam, batched_pts[0].cpu().numpy(), data_dict['batched_calib_info'][0])
            points = torch.from_numpy(points).to(device='cuda', dtype=torch.float32)
            batch_results = model(batched_pts=[points], 
                                  mode='val',
                                  batched_gt_bboxes=batched_gt_bboxes, 
                                  batched_gt_labels=batched_labels)
            # pdb.set_trace()
            for j, result in enumerate(batch_results):
                format_result = {
                    'name': [],
                    'truncated': [],
                    'occluded': [],
                    'alpha': [],
                    'bbox': [],
                    'dimensions': [],
                    'location': [],
                    'rotation_y': [],
                    'score': []
                }
                
                calib_info = data_dict['batched_calib_info'][j]
                tr_velo_to_cam = calib_info['Tr_velo_to_cam_0'].astype(np.float32)
                r0_rect = calib_info['R0_rect'].astype(np.float32)
                P0 = calib_info['P0'].astype(np.float32)
                image_shape = data_dict['batched_img_info'][j]['image_shape']
                idx = data_dict['batched_img_info'][j]['image_idx']
                result_filter = keep_bbox_from_image_range(result, tr_velo_to_cam, r0_rect, P0, image_shape)
                result_filter = keep_bbox_from_lidar_range(result_filter, pcd_limit_range)

                lidar_bboxes = result_filter['lidar_bboxes']
                labels, scores = result_filter['labels'], result_filter['scores']
                bboxes2d, camera_bboxes = result_filter['bboxes2d'], result_filter['camera_bboxes']
                for lidar_bbox, label, score, bbox2d, camera_bbox in \
                    zip(lidar_bboxes, labels, scores, bboxes2d, camera_bboxes):
                    format_result['name'].append(LABEL2CLASSES[label])
                    format_result['truncated'].append(0.0)
                    format_result['occluded'].append(0)
                    alpha = camera_bbox[6] - np.arctan2(camera_bbox[0], camera_bbox[2])
                    format_result['alpha'].append(alpha)
                    format_result['bbox'].append(bbox2d)
                    format_result['dimensions'].append(camera_bbox[3:6])
                    format_result['location'].append(camera_bbox[:3])
                    format_result['rotation_y'].append(camera_bbox[6])
                    format_result['score'].append(score)
                
                write_label(format_result, os.path.join(saved_submit_path, f'{idx:06d}.txt'))

                format_results[idx] = {k:np.array(v) for k, v in format_result.items()}
        
        write_pickle(format_results, os.path.join(saved_path, 'results.pkl'))
    
    print('Evaluating.. Please wait several seconds.')
    do_eval(format_results, val_dataset.data_infos, CLASSES, saved_path)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Configuration Parameters')
    parser.add_argument('--data_root', help='your data root for waymo')
    parser.add_argument('--lidar_detector', default='pretrained/epoch_160.pth', help='your lidar model checkpoint')
    parser.add_argument('--segmentor', help='your segmentation model checkpoint', required=True)
    parser.add_argument('--saved_path', default='results', help='your saved path for predicted results')
    parser.add_argument('--num_workers', type=int, default=4)
    parser.add_argument('--nclasses', type=int, default=3)
    parser.add_argument('--painted', action='store_true', help='if using painted lidar points')
    parser.add_argument('--cam_sync', action='store_true', help='only use objects visible to a camera')
    parser.add_argument('--no_cuda', action='store_true',
                        help='whether to use cuda')
    args = parser.parse_args()

    main(args)