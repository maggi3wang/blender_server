"""
Visualizes SceneGraph state using Blender Server.
"""
from __future__ import print_function
import argparse
import math
import os
import re
import time
import warnings

import matplotlib.pyplot as plt
import numpy as np

from drake import lcmt_viewer_load_robot
from pydrake.common.eigen_geometry import Quaternion
from pydrake.geometry import DispatchLoadMessage, SceneGraph
from pydrake.lcm import DrakeMockLcm
from pydrake.math import RigidTransform, RotationMatrix, RollPitchYaw
from pydrake.systems.framework import (
    AbstractValue, LeafSystem, PublishEvent, TriggerType
)
from pydrake.systems.rendering import PoseBundle
from pydrake.multibody.plant import ContactResults

from pydrake.common import FindResourceOrThrow
from pydrake.examples.manipulation_station import (
    ManipulationStation,
    CreateDefaultYcbObjectList)
from pydrake.manipulation.simple_ui import JointSliders, SchunkWsgButtons
from pydrake.multibody.parsing import Parser
from pydrake.systems.framework import DiagramBuilder
from pydrake.systems.analysis import Simulator
from pydrake.systems.meshcat_visualizer import MeshcatVisualizer
from pydrake.systems.primitives import FirstOrderLowPassFilter
from pydrake.util.eigen_geometry import Isometry3


from blender_server.blender_server_interface.blender_server_interface import BlenderServerInterface

def reorder_quat_wxyz_to_xyzw(quat):
    if isinstance(quat, np.ndarray):
        quat = quat.tolist()
    return [quat[1], quat[2], quat[3], quat[0]]

class BlenderColorCamera(LeafSystem):
    """
    BlenderColorCamera is a System block that connects to the pose bundle output
    port of a SceneGraph and uses BlenderServer to render a color camera image.
    """

    def __init__(self,
                 scene_graph,
                 zmq_url="default",
                 draw_period=0.033333,
                 camera_tfs=[Isometry3()],
                 material_overrides=[],
                 global_transform=Isometry3()):
        """
        Args:
            scene_graph: A SceneGraph object.
            draw_period: The rate at which this class publishes rendered
                images.
            zmq_url: Optionally set a url to connect to the blender server.
                Use zmp_url="default" to the value obtained by running a
                single blender server in another terminal.
                TODO(gizatt): Use zmp_url=None or zmq_url="new" to start a
                new server (as a child of this process); a new web browser
                will be opened (the url will also be printed to the console).
                Use e.g. zmq_url="tcp://127.0.0.1:5556" to specify a
                specific address.
            camera tfs: List of Isometry3 camera tfs.
            material_overrides: A list of tuples of regex rules and
                material override arguments to be passed into a
                a register material call, not including names. (Those are
                assigned automatically by this class.)
            global transform: Isometry3 that gets premultiplied to every object.

        Note: This call will not return until it connects to the
              Blender server.
        """
        LeafSystem.__init__(self)

        self.current_publish_num = 0
        self.set_name('blender_color_camera')
        self._DeclarePeriodicPublish(draw_period, 0.0)
        self.draw_period = draw_period

        # Pose bundle (from SceneGraph) input port.
        self._DeclareAbstractInputPort("lcm_visualization",
                                       AbstractValue.Make(PoseBundle(0)))

        if zmq_url == "default":
            zmq_url = "tcp://127.0.0.1:5556"
        elif zmq_url == "new":
            raise NotImplementedError("TODO")

        # Connect to the server via a 
        if zmq_url is not None:
            print("Connecting to blender server at zmq_url=" + zmq_url + "...")
        self.bsi = BlenderServerInterface(zmq_url=zmq_url)
        print("Connected to Blender server.")
        self._scene_graph = scene_graph

        # Compile regex for the material overrides
        self.material_overrides = [
            (re.compile(x[0]), x[1]) for x in material_overrides]
        self.global_transform = global_transform
        self.camera_tfs = camera_tfs
        def on_initialize(context, event):
            self.load()

        self._DeclareInitializationEvent(
            event=PublishEvent(
                trigger_type=TriggerType.kInitialization,
                callback=on_initialize))

        plt.figure()

    def _parse_name(self, name):
        # Parse name, split on the first (required) occurrence of `::` to get
        # the source name, and let the rest be the frame name.
        # TODO(eric.cousineau): Remove name parsing once #9128 is resolved.
        delim = "::"
        assert delim in name
        pos = name.index(delim)
        source_name = name[:pos]
        frame_name = name[pos + len(delim):]
        return source_name, frame_name

    def _format_geom_name(self, source_name, frame_name, model_id, i):
        return "{}::{}::{}::{}".format(source_name, model_id, frame_name, i)

    def load(self):
        """
        Loads all visualization elements in the Blender server.

        @pre The `scene_graph` used to construct this object must be part of a
        fully constructed diagram (e.g. via `DiagramBuilder.Build()`).
        """
        self.bsi.send_remote_call("initialize_scene")

        # Intercept load message via mock LCM.
        mock_lcm = DrakeMockLcm()
        DispatchLoadMessage(self._scene_graph, mock_lcm)
        load_robot_msg = lcmt_viewer_load_robot.decode(
            mock_lcm.get_last_published_message("DRAKE_VIEWER_LOAD_ROBOT"))
        # Load all the elements over on the Blender side.
        self.num_link_geometries_by_link_name = {}
        self.link_subgeometry_local_tfs_by_link_name = {}
        for i in range(load_robot_msg.num_links):
            link = load_robot_msg.link[i]
            [source_name, frame_name] = self._parse_name(link.name)
            self.num_link_geometries_by_link_name[link.name] = link.num_geom

            tfs = []
            for j in range(link.num_geom):
                geom = link.geom[j]

                # MultibodyPlant currently sets alpha=0 to make collision
                # geometry "invisible".  Ignore those geometries here.
                if geom.color[3] == 0:
                    continue

                geom_name = self._format_geom_name(source_name, frame_name, link.robot_num, j)

                tfs.append(RigidTransform(
                    RotationMatrix(Quaternion(geom.quaternion)),
                    geom.position).GetAsMatrix4())

                # It will have a material with this key.
                # We will set it up after deciding whether it's
                # a mesh with a texture or not...
                material_key = "material_" + geom_name
                material_key_assigned = False

                # Check overrides
                for override in self.material_overrides:
                    if override[0].match(geom_name):
                        print("Using override ", override[0].pattern, " on name ", geom_name, " with applied args ", override[1])
                        self.bsi.send_remote_call(
                            "register_material",
                            name=material_key,
                            **(override[1]))
                        material_key_assigned = True

                if geom.type == geom.BOX:
                    assert geom.num_float_data == 3
                    # Blender cubes are 2x2x2 by default
                    do_load_geom = lambda: self.bsi.send_remote_call(
                        "register_object",
                        name="obj_" + geom_name,
                        type="cube",
                        scale=[x*0.5 for x in geom.float_data[:3]],
                        location=geom.position,
                        quaternion=geom.quaternion,
                        material=material_key)

                elif geom.type == geom.SPHERE:
                    assert geom.num_float_data == 1
                    do_load_geom = lambda: self.bsi.send_remote_call(
                        "register_object",
                        name="obj_" + geom_name,
                        type="sphere",
                        scale=geom.float_data[0],
                        location=geom.position,
                        quaternion=geom.quaternion,
                        material=material_key)

                elif geom.type == geom.CYLINDER:
                    assert geom.num_float_data == 2
                    # Blender cylinders are r=1, h=2 by default
                    do_load_geom = lambda: self.bsi.send_remote_call(
                        "register_object",
                        name="obj_" + geom_name,
                        type="cylinder",
                        scale=[geom.float_data[0], geom.float_data[0], 0.5*geom.float_data[1]],
                        location=geom.position,
                        quaternion=geom.quaternion,
                        material=material_key)

                elif geom.type == geom.MESH:
                    do_load_geom = lambda: self.bsi.send_remote_call(
                        "register_object",
                        name="obj_" + geom_name,
                        type="obj",
                        path=geom.string_data[0:-3] + "obj",
                        scale=geom.float_data[:3],
                        location=geom.position,
                        quaternion=geom.quaternion,
                        material=material_key)

                    # Attempt to find a texture for the object by looking for an
                    # identically-named *.png next to the model.
                    # TODO(gizatt): In the long term, this kind of material information
                    # should be gleaned from the SceneGraph constituents themselves, so
                    # that we visualize what the simulation is *actually* reasoning about
                    # rather than what files happen to be present.
                    candidate_texture_path_png = geom.string_data[0:-3] + "png"
                    if not material_key_assigned and os.path.exists(candidate_texture_path_png):
                        material_key_assigned = self.bsi.send_remote_call(
                            "register_material",
                            name=material_key,
                            material_type="color_texture",
                            path=candidate_texture_path_png)

                else:
                    print("UNSUPPORTED GEOMETRY TYPE {} IGNORED".format(
                          geom.type))
                    continue

                if not material_key_assigned:
                    material_key_assigned = self.bsi.send_remote_call(
                        "register_material",
                        name=material_key,
                        material_type="color",
                        color=geom.color[:4])

                # Finally actually load the geometry now that the material is registered.
                do_load_geom()

            self.link_subgeometry_local_tfs_by_link_name[link.name] = tfs

        for i, camera_tf in enumerate(self.camera_tfs):
            camera_tf_post = self.global_transform.multiply(camera_tf)
            self.bsi.send_remote_call(
                "register_camera",
                name="cam_%d" % i,
                location=camera_tf_post.translation().tolist(),
                quaternion=camera_tf_post.quaternion().wxyz().tolist(),
                angle=90)

            self.bsi.send_remote_call(
                "configure_rendering",
                camera_name='cam_%d' % i,
                resolution=[1280, 720],
                file_format="JPEG")

        env_map_path = "/home/gizatt/tools/blender_server/data/env_maps/shanghai_bund_4k.hdr"
        self.bsi.send_remote_call(
            "set_environment_map",
            path=env_map_path)

    def _DoPublish(self, context, event):
        # TODO(russt): Change this to declare a periodic event with a
        # callback instead of overriding _DoPublish, pending #9992.
        LeafSystem._DoPublish(self, context, event)

        pose_bundle = self.EvalAbstractInput(context, 0).get_value()

        for frame_i in range(pose_bundle.get_num_poses()):
            link_name = pose_bundle.get_name(frame_i)
            [source_name, frame_name] = self._parse_name(link_name)
            model_id = pose_bundle.get_model_instance_id(frame_i)
            pose_matrix = pose_bundle.get_pose(frame_i)
            for j in range(self.num_link_geometries_by_link_name[link_name]):
                offset = Isometry3(
                    self.global_transform.matrix().dot(
                        pose_matrix.matrix().dot(
                            self.link_subgeometry_local_tfs_by_link_name[link_name][j])))
                location = offset.translation()
                quaternion = offset.quaternion().wxyz()
                geom_name = self._format_geom_name(source_name, frame_name, model_id, j)
                self.bsi.send_remote_call(
                    "update_parameters",
                    name="obj_" + geom_name,
                    location=location.tolist(),
                    rotation_mode='QUATERNION',
                    rotation_quaternion=quaternion.tolist())

        n_cams = len(self.camera_tfs)
        for i in range(len(self.camera_tfs)):
            plt.subplot(1, n_cams, i+1)
            im = self.bsi.render_image(
                "cam_%d" % i,
                filepath="/tmp/drake_blender_vis_scene_render_%02d_%08d.jpg" % (i, self.current_publish_num))
            plt.imshow(im)

        if self.current_publish_num == 0:
            self.bsi.send_remote_call("save_current_scene", path="/tmp/drake_blender_vis_scene.blend")
            self.published_scene = True
        self.current_publish_num += 1
        #self.bsi.send_remote_call("save_current_scene", path="/tmp/drake_blender_vis_scene_%d.blend" % (time.time()*1000*1000))

        plt.pause(0.01)


if __name__ == "__main__":
    builder = DiagramBuilder()

    
    station = builder.AddSystem(ManipulationStation())
    station.SetupDefaultStation()

    new_obj_list = [
        ['drake/manipulation/models/ycb/sdf/003_cracker_box.sdf',
         RigidTransform(rpy=RollPitchYaw([0.72, -12.3, 0.7]), p=[0.55, 0.2, 0.2])],
        ['drake/manipulation/models/ycb/sdf/004_sugar_box.sdf',
         RigidTransform(rpy=RollPitchYaw([1.1, 0.5, -0.6]), p=[0.45, 0.10, 0.15])],
        ['drake/manipulation/models/ycb/sdf/005_tomato_soup_can.sdf',
         RigidTransform(rpy=RollPitchYaw([1.2, 0., 0.3]), p=[0.4, -0.2, 0.15])],
        ['drake/manipulation/models/ycb/sdf/006_mustard_bottle.sdf',
         RigidTransform(rpy=RollPitchYaw([1.8, 5.2, -2.]), p=[0.3, -0.1, 0.1])],
        ['drake/manipulation/models/ycb/sdf/009_gelatin_box.sdf',
         RigidTransform(rpy=RollPitchYaw([1.8, 5.2, -2.]), p=[0.35, 0.05, 0.1])]
    ]

    for pair in new_obj_list:
        station.AddManipulandFromFile(
            model_file=pair[0],
            X_WObject=pair[1])
    station.Finalize()

    #meshcat = builder.AddSystem(MeshcatVisualizer(
    #        station.get_scene_graph()))
    #builder.Connect(station.GetOutputPort("pose_bundle"),
    #                meshcat.get_input_port(0))


    #cam_quat_base = RollPitchYaw(
    #    81.8*np.pi/180.,
    #    -5.*np.pi/180,
    #    (129.+90)*np.pi/180.).ToQuaternion().wxyz()
    #cam_trans_base = [-0.196, 0.816, 0.435]
    #cam_tf_base = Isometry3(translation=cam_trans_base,
    #                        quaternion=cam_quat_base)
    #cam_tfs = [cam_tf_base]

    cam_tfs = []
    for cam_name in [u"0", u"1", u"2"]:
        cam_tf_base = station.GetStaticCameraPosesInWorld()[cam_name].GetAsIsometry3()
        # Rotate cam to get it into blender +y up, +x right, -z forward
        cam_tf_base.set_rotation(cam_tf_base.matrix()[:3, :3].dot(
            RollPitchYaw([-np.pi/2, 0., np.pi/2]).ToRotationMatrix().matrix()))
        cam_tfs.append(cam_tf_base)

    offset_quat_base = RollPitchYaw(0., 0., np.pi/2).ToQuaternion().wxyz()
    blender_cam = builder.AddSystem(BlenderColorCamera(
        station.get_scene_graph(),
        draw_period=0.0333,
        camera_tfs=cam_tfs,
        material_overrides=[
            (".*amazon_table.*",
                {"material_type": "CC0_texture",
                 "path": "data/test_pbr_mats/Metal09/Metal09"}),
            (".*cupboard_body.*",
                {"material_type": "CC0_texture",
                 "path": "data/test_pbr_mats/Wood15/Wood15"}),
            (".*cupboard.*door.*",
                {"material_type": "CC0_texture",
                 "path": "data/test_pbr_mats/Wood08/Wood08"})
        ],
        global_transform=Isometry3(translation=[0, 0, 0],
                                   quaternion=Quaternion(offset_quat_base))
    ))
    builder.Connect(station.GetOutputPort("pose_bundle"),
                    blender_cam.get_input_port(0))

    teleop = builder.AddSystem(JointSliders(station.get_controller_plant(),
                                            length=800))

    filter = builder.AddSystem(FirstOrderLowPassFilter(time_constant=2.0,
                                                       size=7))
    builder.Connect(teleop.get_output_port(0), filter.get_input_port(0))
    builder.Connect(filter.get_output_port(0),
                    station.GetInputPort("iiwa_position"))

    wsg_buttons = builder.AddSystem(SchunkWsgButtons(teleop.window))
    builder.Connect(wsg_buttons.GetOutputPort("position"), station.GetInputPort(
        "wsg_position"))
    builder.Connect(wsg_buttons.GetOutputPort("force_limit"),
                    station.GetInputPort("wsg_force_limit"))

    diagram = builder.Build()
    simulator = Simulator(diagram)

    station_context = diagram.GetMutableSubsystemContext(
        station, simulator.get_mutable_context())

    station_context.FixInputPort(station.GetInputPort(
        "iiwa_feedforward_torque").get_index(), np.zeros(7))

    # Eval the output port once to read the initial positions of the IIWA.
    q0 = station.GetOutputPort("iiwa_position_measured").Eval(
        station_context)
    q0[:] = [0., 0.5, 0., -1.75, 0., 1., 0.]
    teleop.set_position(q0)
    filter.set_initial_output_value(diagram.GetMutableSubsystemContext(
        filter, simulator.get_mutable_context()), q0)

    # This is important to avoid duplicate publishes to the hardware interface:
    simulator.set_publish_every_time_step(False)

    simulator.set_target_realtime_rate(1.0)
    simulator.StepTo(100.0)
