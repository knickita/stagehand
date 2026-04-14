import bpy
from mathutils import Vector, Quaternion

addon_keymaps = []

class FirstPersonLook(bpy.types.Operator):
    bl_idname = "view3d.fps_rmb_wasd"
    bl_label = "first person look RMB+WASD (Hold)"
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        if not (context.area and context.area.type == 'VIEW_3D'):
            return {'CANCELLED'}

        prefs = get_prefs()
        self.speed = prefs.speed
        self.look_sens = prefs.look_sens

        self.rv3d = context.space_data.region_3d
        self.rmb_held = True  # invoked by RMB (or Alt+RMB) press

        self.keys = {
            "W": False, "A": False, "S": False, "D": False,
            "Q": False, "E": False
        }
        self.last_x = event.mouse_x
        self.last_y = event.mouse_y

        self.xMin=context.region.x
        self.xMax=self.xMin+context.region.width
        self.yMin=context.region.y
        self.yMax=self.yMin+context.region.height

        self.skipNextMove=False

        self._timer = context.window_manager.event_timer_add(1.0 / 60.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def finish(self, context):
        wm = context.window_manager
        if getattr(self, "_timer", None) is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        context.window.cursor_modal_restore()

    def modal(self, context, event):
        # Stop when RMB released
        if event.type == 'RIGHTMOUSE' and event.value == 'RELEASE':
            self.finish(context)
            return {'FINISHED'}

        # Track key state
        if event.type in self.keys:
            self.keys[event.type] = (event.value != 'RELEASE')
            return {'RUNNING_MODAL'}

        # Mouse look (Invert Y + rotate around camera/eye position)
        if event.type == 'MOUSEMOVE':
            if self.skipNextMove:
                self.skipNextMove=False
                self.last_x=event.mouse_x
                self.last_y=event.mouse_y
                return {'RUNNING_MODAL'}
            
            dx = event.mouse_x - self.last_x
            dy = event.mouse_y - self.last_y

            #wrap mouse inside context window
            if (event.mouse_x<=self.xMin):
                bpy.context.window.cursor_warp(self.xMax-1,event.mouse_y)
                self.skipNextMove=True
            elif (event.mouse_x>=self.xMax):
                bpy.context.window.cursor_warp(self.xMin+1,event.mouse_y)
                self.skipNextMove=True
            if (event.mouse_y<=self.yMin):
                bpy.context.window.cursor_warp(event.mouse_x,self.yMax-1)
                self.skipNextMove=True
            elif(event.mouse_y>=self.yMax):
                bpy.context.window.cursor_warp(event.mouse_x,self.yMin+1)
                self.skipNextMove=True
            
            self.last_x = event.mouse_x
            self.last_y = event.mouse_y

            rv3d = self.rv3d

            if not rv3d.is_perspective:
                rv3d.view_perspective="PERSP"

            # Save the current eye position (camera position)
            dist = rv3d.view_distance
            eye = rv3d.view_location + (rv3d.view_rotation @ Vector((0, 0, dist)))

            # yaw around global Z
            yaw = Quaternion((0, 0, 1), -dx * self.look_sens)

            # pitch around view-local X (INVERTED Y: note the +dy instead of -dy)
            pitch_axis = rv3d.view_rotation @ Vector((1, 0, 0))
            pitch = Quaternion(pitch_axis, +dy * self.look_sens)

            new_rot = (yaw @ pitch) @ rv3d.view_rotation
            rv3d.view_rotation = new_rot

            # Recompute view_location so the eye stays fixed (FPS rotate-in-place)
            rv3d.view_location = eye - (new_rot @ Vector((0, 0, dist)))

            return {'RUNNING_MODAL'}


        # Movement update
        if event.type == 'TIMER':
            rv3d = self.rv3d
            dt = 1.0 / 60.0
            spd = self.speed * (3.0 if event.shift else 1.0)

            forward = rv3d.view_rotation @ Vector((0, 0, -1))
            right   = rv3d.view_rotation @ Vector((1, 0, 0))
            up      = rv3d.view_rotation @ Vector((0, 1, 0))  # local up

            move = Vector((0, 0, 0))
            if self.keys["W"]: move += forward
            if self.keys["S"]: move -= forward
            if self.keys["D"]: move += right
            if self.keys["A"]: move -= right
            if self.keys["E"]: move += up      # up
            if self.keys["Q"]: move -= up      # down

            if move.length_squared != 0:
                rv3d.view_location += move.normalized() * spd * dt

            return {'RUNNING_MODAL'}

        return {'PASS_THROUGH'}


def register_keymap():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    prefs = get_prefs()

    km = kc.keymaps.new(name='3D View', space_type='VIEW_3D')

    # RMB PRESS starts the operator; operator ends on RMB RELEASE.
    kmi = km.keymap_items.new(
        FirstPersonLook.bl_idname,
        type='RIGHTMOUSE',
        value='PRESS',  
        alt=prefs.use_alt
    )

    addon_keymaps.append((km, kmi))


def get_prefs():
    class Fallback:
        speed = 10.0
        look_sens = 0.002
        use_alt = False
    return Fallback()


def unregister_keymap():
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()


def register():    
    bpy.utils.register_class(FirstPersonLook)
    register_keymap()


def unregister():
    unregister_keymap()
    if hasattr(FirstPersonLook, "bl_rna"):
        bpy.utils.unregister_class(FirstPersonLook)