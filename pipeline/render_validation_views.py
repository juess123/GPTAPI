"""Render neutral, deterministic views of the generated model for visual validation."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import bpy
from mathutils import Vector


OUT_DIR = Path(os.environ["VALIDATION_VIEWS_DIR"]).resolve()
OUT_DIR.mkdir(parents=True, exist_ok=True)
GEOMETRY_TYPES = {"MESH", "CURVE", "FONT", "SURFACE", "META"}


def bounds_of(objects):
    points = []
    for obj in objects:
        if obj.type not in GEOMETRY_TYPES or not obj.bound_box:
            continue
        points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
    if not points:
        raise RuntimeError("场景中没有可用于视觉验证的几何对象")
    low = Vector(tuple(min(point[index] for point in points) for index in range(3)))
    high = Vector(tuple(max(point[index] for point in points) for index in range(3)))
    return low, high


def choose_objects():
    geometry = [obj for obj in bpy.context.scene.objects if obj.type in GEOMETRY_TYPES]
    classified = [obj for obj in geometry
                  if bool(obj.get("quote_relevant", False))
                  and not bool(obj.get("exclude_from_quote", False))]
    return classified or [obj for obj in geometry if not obj.hide_render]


def choose_engine(scene):
    try:
        engines = bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items.keys()
    except Exception:
        engines = []
    for name in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "BLENDER_WORKBENCH", "CYCLES"):
        if name in engines:
            try:
                scene.render.engine = name
                return name
            except Exception:
                continue
    return str(scene.render.engine)


def point_camera(camera, location, target):
    camera.location = location
    camera.rotation_euler = (target - location).to_track_quat("-Z", "Y").to_euler()


def add_area_light(collection, name, location, energy, size, target):
    data = bpy.data.lights.new(name=name, type="AREA")
    data.energy = energy
    data.shape = "DISK"
    data.size = size
    obj = bpy.data.objects.new(name, data)
    collection.objects.link(obj)
    obj.location = location
    obj.rotation_euler = (target - location).to_track_quat("-Z", "Y").to_euler()
    return obj


scene = bpy.context.scene
objects = choose_objects()
low, high = bounds_of(objects)
center = (low + high) * 0.5
size = high - low
largest = max(size.x, size.y, size.z, 0.1)

# Hide cameras, lights, floors and studios from validation without saving changes.
selected_names = {obj.name for obj in objects}
for obj in scene.objects:
    if obj.name not in selected_names and obj.type in GEOMETRY_TYPES:
        obj.hide_render = True
    if obj.type in {"LIGHT", "CAMERA"}:
        obj.hide_render = True

collection = bpy.data.collections.new("__VALIDATION_ONLY__")
scene.collection.children.link(collection)
camera_data = bpy.data.cameras.new("ValidationCamera")
camera = bpy.data.objects.new("ValidationCamera", camera_data)
collection.objects.link(camera)
camera.hide_render = False
scene.camera = camera

engine = choose_engine(scene)
scene.render.resolution_x = 900
scene.render.resolution_y = 900
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"
scene.render.film_transparent = False
scene.render.use_file_extension = True
if hasattr(scene.render, "use_compositing"):
    scene.render.use_compositing = False

world = scene.world or bpy.data.worlds.new("ValidationWorld")
scene.world = world
try:
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background") if world.node_tree else None
    if background:
        if "Color" in background.inputs:
            background.inputs["Color"].default_value = (0.055, 0.055, 0.065, 1.0)
        if "Strength" in background.inputs:
            background.inputs["Strength"].default_value = 0.35
except Exception:
    world.color = (0.055, 0.055, 0.065)

light_distance = largest * 2.2
add_area_light(collection, "ValidationKey",
               center + Vector((-light_distance, -light_distance, light_distance * 1.3)),
               1400.0, largest * 1.5, center)
add_area_light(collection, "ValidationFill",
               center + Vector((light_distance, -light_distance * 0.4, light_distance * 0.7)),
               900.0, largest * 1.2, center)
add_area_light(collection, "ValidationRim",
               center + Vector((0.0, light_distance, light_distance)),
               1100.0, largest, center)

views = [
    ("front", Vector((center.x, low.y - largest * 2.5, center.z)), True),
    ("back", Vector((center.x, high.y + largest * 2.5, center.z)), True),
    ("left", Vector((low.x - largest * 2.5, center.y, center.z)), True),
    ("right", Vector((high.x + largest * 2.5, center.y, center.z)), True),
    ("perspective", center + Vector((largest * 1.7, -largest * 2.2, largest * 1.35)), False),
]
rendered = []
for name, location, orthographic in views:
    point_camera(camera, location, center)
    camera.data.type = "ORTHO" if orthographic else "PERSP"
    if orthographic:
        if name in {"front", "back"}:
            camera.data.ortho_scale = max(size.x, size.z) * 1.18
        else:
            camera.data.ortho_scale = max(size.y, size.z) * 1.18
    else:
        camera.data.lens = 52
    path = OUT_DIR / f"validation_{name}.png"
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    rendered.append(str(path))

manifest = {
    "schema": 1,
    "engine": engine,
    "views": rendered,
    "objects": [obj.name for obj in objects],
    "bounds_m": {"min": list(low), "max": list(high), "size": list(size)},
    "note": "These views are neutral validation evidence; lighting and camera are not acceptance criteria.",
}
(OUT_DIR / "views_manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"VALIDATION_VIEWS={OUT_DIR}")
print(f"VALIDATION_VIEW_COUNT={len(rendered)}")
