# Blender 2.93
# blender -noaudio --background --python render_pix3d.py -- -o data/pix3d_renders

# based on:
# https://github.com/weiaicunzai/blender_shapenet_render/blob/master/render_depth.py
# https://github.com/xingyuansun/pix3d/blob/master/demo.py

import os
import sys
import json
import math
import random
import argparse

import bpy
import mathutils

def set_camera_location_rotation(azimuth, elevation, distance, tilt):
    # render z pass ; render a object z pass map by a given camera viewpoints
    # args in degrees/meters (object centered)
    azimuth, elevation, distance, tilt = map(float, [azimuth, elevation, distance, tilt])
    camera = bpy.data.objects['Camera']
    
    phi = elevation * math.pi / 180 
    theta = azimuth * math.pi / 180
    x = distance * math.cos(phi) * math.cos(theta)
    y = distance * math.cos(phi) * math.sin(theta)
    z = distance * math.sin(phi)
    camera.location = (x, y, z)

    x, y, z = 90, 0, 90 #set camera at x axis facing towards object
    x = x - elevation   #latitude
    z = z + azimuth     #longtitude
    camera.rotation_euler = (x * math.pi / 180, y * math.pi / 180, z * math.pi / 180)

def configure_camera(camera_obj, lens):
    camera_obj.location = (0, 0, 0)
    camera_obj.rotation_euler = (0, math.pi, 0)
    camera_obj.data.type = 'PERSP'
    camera_obj.data.sensor_width = 32
    camera_obj.data.sensor_height = 18
    camera_obj.data.sensor_fit = 'HORIZONTAL'
    camera_obj.data.lens = data['focal_length']

def configure_scene_render(scene_render, resolution_x, resolution_y, tiles, color_mode, color_depth):
    scene_render.engine = 'CYCLES'
    scene_render.image_settings.file_format = 'PNG'
    scene_render.use_overwrite = True
    scene_render.use_file_extension = True
    scene_render.resolution_x = resolution_x
    scene_render.resolution_y = resolution_y
    scene_render.resolution_percentage = 100
    scene_render.tile_x = tiles
    scene_render.tile_y = tiles 
    scene_render.image_settings.color_mode = color_mode
    scene_render.image_settings.color_depth = color_depth
    
def enable_gpu(use_gpu):
    if use_gpu:
        #bpy.context.user_preferences.addons['cycles'].preferences.devices[0].use = True
        #bpy.context.user_preferences.addons['cycles'].preferences.compute_device_type = 'CUDA'
        bpy.types.CyclesRenderSettings.device = 'GPU'
        bpy.data.scenes[bpy.context.scene.name].cycles.device = 'GPU'

def init_camera_scene_regular(n_samples = 5):
    camera_obj = bpy.data.objects['Camera']
    camera_obj.data.clip_end = 1e10
    
    cycles = bpy.context.scene.cycles
    cycles.use_progressive_refine = True
    cycles.samples = n_samples
    cycles.max_bounces = 100
    cycles.min_bounces = 10
    cycles.caustics_reflective = False
    cycles.caustics_refractive = False
    cycles.diffuse_bounces = 10
    cycles.glossy_bounces = 4
    cycles.transmission_bounces = 4
    cycles.volume_bounces = 0
    cycles.transparent_min_bounces = 8
    cycles.transparent_max_bounces = 64
    cycles.blur_glossy = 5
    cycles.sample_clamp_indirect = 5
    
    world = bpy.data.worlds['World']
    world.cycles.sample_as_light = True
    world.use_nodes = True
    world.node_tree.nodes.remove(world.node_tree.nodes['Background']) if 'Background' in world.node_tree.nodes else None

def init_camera_scene_depth(color_mode, color_depth, clip_start = 0.5, clip_end = 4.0):
    camera = bpy.data.cameras['Camera']
    camera.clip_start = clip_start
    camera.clip_end = clip_end
    
    bpy.context.scene.use_nodes = True
    tree = bpy.context.scene.node_tree
    links = tree.links
    for node in tree.nodes:
        tree.nodes.remove(node)
    render_layer_node = tree.nodes.new('CompositorNodeRLayers')
    map_value_node = tree.nodes.new('CompositorNodeMapValue')
    file_output_node = tree.nodes.new('CompositorNodeOutputFile')
    map_value_node.offset[0] = -clip_start
    map_value_node.size[0] = 1 / (clip_end - clip_start)
    map_value_node.use_min = True
    map_value_node.use_max = True
    map_value_node.min[0] = 0.0
    map_value_node.max[0] = 1.0
    file_output_node.format.color_mode = color_mode
    file_output_node.format.color_depth = color_depth
    file_output_node.format.file_format = 'PNG' 
    links.new(render_layer_node.outputs[2], map_value_node.inputs[0])
    links.new(map_value_node.outputs[0], file_output_node.inputs[0])

    return file_output_node

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input-path', '-i', default = 'data/common/pix3d/pix3d.json')
    parser.add_argument('--output-path', '-o', default = 'data/pix3d_renders')
    parser.add_argument('--viewpoints-path', default = 'pix3d_clustered_viewpoints.json')
    parser.add_argument('--seed', type = int, default = 42)
    args = parser.parse_args(sys.argv[1 + sys.argv.index('--'):] if '--' in sys.argv else [])

    random.seed(args.seed)

    meta = json.load(open(args.input_path))
    viewpoints_by_category = json.load(open(args.viewpoints_path))
    model_paths = sorted(set(m['model'] for m in meta))
    
    data = meta[0]
    #w, h = data['img_size']
    #f = data['focal_length']
    w, h = 640, 480
    f = 35
    
    hilbert_spiral = 512
    color_mode, color_depth = 'BW', '8'
    
    bpy.context.scene.render.engine = 'CYCLES'
    world = bpy.data.worlds['World']
    world.light_settings.use_ambient_occlusion = True
    world.light_settings.ao_factor = 0.9

    scene = bpy.data.scenes[bpy.context.scene.name]
    configure_scene_render(scene.render, w, h, hilbert_spiral, color_mode = color_mode, color_depth = color_depth)
    
    configure_camera(bpy.data.objects['Camera'], f)
    bpy.context.scene.camera = bpy.data.objects['Camera']
    
    #file_output_node = init_camera_scene_depth(color_mode = color_mode, color_depth = color_depth)
    init_camera_scene_regular()

    enable_gpu(use_gpu = False)
    
    for i, model_path in enumerate(model_paths):
        print(i, '/', len(model_paths), model_path)
        model_dir = os.path.join(args.output_path, os.path.dirname(model_path))
        category = os.path.basename(os.path.dirname(os.path.dirname(model_path)))
        os.makedirs(model_dir, exist_ok = True)
 
        bpy.ops.object.select_all(action = 'DESELECT')
        for obj in bpy.data.objects:
            if obj.type == 'MESH':
                obj.select_set(True)
        bpy.ops.object.delete()
        
        bpy.ops.import_scene.obj(filepath=os.path.join(os.path.dirname(args.input_path), model_path), axis_forward='-Z', axis_up='Y')
        obj = bpy.context.selected_objects[0]
        for k in range(len(viewpoints_by_category[category]['rot_mat'])):
            frame_path = os.path.join(model_dir, 'view-{:06}.png'.format(1 + k))
            #trans_vec = random.choice(viewpoints_by_category[category]['trans_vec'])
            trans_vec = viewpoints_by_category[category]['trans_vec'][0]
            rot_mat = viewpoints_by_category[category]['rot_mat'][k]
            quat = viewpoints_by_category[category]['quat'][k]

            #obj.location = trans_vec
            #obj.rotation_quaternion = quat
            obj.matrix_world = mathutils.Matrix.Translation(trans_vec) @ mathutils.Matrix(rot_mat).to_4x4()
            
            #file_output_node.base_path = os.path.dirname(frame_path)
            #file_output_node.file_slots[0].path = 'view-######.png'
            #bpy.ops.render.render(write_still = False)
    
            bpy.context.scene.render.filepath = frame_path
            bpy.ops.render.render(write_still = True)

            bpy.context.scene.frame_set(1 + bpy.context.scene.frame_current)

            print(frame_path)
        break