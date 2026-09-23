# 用 Blender 无界面重新打开成品，并输出场景事实。不做任何修改。
import bpy, sys, os

path = os.environ.get("BLEND_IN", "")
print("=" * 62)
print("Blender 重新打开:", path)
bpy.ops.wm.open_mainfile(filepath=path)
print("打开成功，未报错")
print("=" * 62)

scene = bpy.context.scene
objs = list(bpy.data.objects)
print(f"CHK_OBJECTS={len(objs)}")
print(f"CHK_MESHES={len([o for o in objs if o.type=='MESH'])}")
print(f"CHK_LIGHTS={len([o for o in objs if o.type=='LIGHT'])}")
print(f"CHK_CAMERAS={len([o for o in objs if o.type=='CAMERA'])}")
print(f"CHK_EMPTIES={len([o for o in objs if o.type=='EMPTY'])}")
print(f"CHK_MATERIALS={len(bpy.data.materials)}")
print(f"CHK_COLLECTIONS={len(bpy.data.collections)}")
print(f"CHK_ACTIONS={len(bpy.data.actions)}")
print(f"CHK_TEXTS={len(bpy.data.texts)}")
print("CHK_TEXTS_NAME=" + ";".join(t.name for t in bpy.data.texts))
print(f"CHK_ENGINE={scene.render.engine}")
print(f"CHK_UNITS={scene.unit_settings.system}/{scene.unit_settings.scale_length}")
print(f"CHK_RES={scene.render.resolution_x}x{scene.render.resolution_y}")
print(f"CHK_FRAMES={scene.frame_start}-{scene.frame_end}")

# 世界包围盒
import mathutils
lo = mathutils.Vector((1e18,) * 3)
hi = mathutils.Vector((-1e18,) * 3)
n = 0
for o in objs:
    if o.type != "MESH":
        continue
    n += 1
    for c in o.bound_box:
        w = o.matrix_world @ mathutils.Vector(c)
        for i in range(3):
            lo[i] = min(lo[i], w[i])
            hi[i] = max(hi[i], w[i])
if n:
    d = [hi[i] - lo[i] for i in range(3)]
    print(f"CHK_BBOX_M={d[0]:.4f},{d[1]:.4f},{d[2]:.4f}")
    print(f"CHK_BBOX_MM={d[0]*1000:.1f},{d[1]*1000:.1f},{d[2]*1000:.1f}")
else:
    print("CHK_BBOX_MM= 无网格")

print("CHK_OBJECT_NAMES=" + ";".join(o.name for o in objs))
print("CHK_MAT_NAMES=" + ";".join(m.name for m in bpy.data.materials))

# 三角形数量
tris = 0
for o in objs:
    if o.type == "MESH" and o.data:
        o.data.calc_loop_triangles()
        tris += len(o.data.loop_triangles)
print(f"CHK_TRIS={tris}")
print("CHK_DONE=1")
