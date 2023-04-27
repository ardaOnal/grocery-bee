import logging
import numpy as np
import os
import pydot
from IPython.display import HTML, SVG, display

from pydrake.all import (DiagramBuilder, MeshcatVisualizer, PortSwitch, Simulator, StartMeshcat)

from manipulation import running_as_notebook
from manipulation.scenarios import  ycb, MakeManipulationStation, AddIiwaDifferentialIK
from manipulation.meshcat_utils import MeshcatPoseSliders
                                    
#import scenarios
#from scenarios import JOINT_COUNT
import grasp_selector
import nodiffik_warnings
import planner as planner_class
import helpers

JOINT_COUNT = 7
SAVE_DIAGRAM = True

logging.getLogger("drake").addFilter(nodiffik_warnings.NoDiffIKWarnings())

# Start the visualizer.
meshcat = StartMeshcat()

rs = np.random.RandomState()

PREPICK_DISTANCE = 0.12
ITEM_COUNT = 5  # number of items to be generated
MAX_TIME = 160  # max duration after which the simulation is forced to end (recommended: ITEM_COUNT * 31)


def generate_environment():
    model_directives = """
directives:
- add_directives:
    file: package://grocery/two_bins_w_cameras.dmd.yaml
- add_directives:
    file: package://grocery/iiwa_and_wsg_with_collision.dmd.yaml
"""
    i = 0
    # generate ITEM_COUNT items
    while i < ITEM_COUNT:
        object_num = rs.randint(len(ycb))
        if "cracker_box" in ycb[object_num] or "mustard" in ycb[object_num] or "sugar" in ycb[object_num]:
            # skip it. it's just too big!
            continue
        model_directives += f"""
- add_model:
    name: ycb{i}
    file: package://grocery/meshes/{ycb[object_num]}
"""
        i += 1

    diagram = MakeManipulationStation(model_directives, time_step=0.001,
                                      package_xmls=[os.path.join(os.path.dirname(
                                          os.path.realpath(__file__)), "models/package.xml")])
    return diagram


def clutter_clearing_demo():
    meshcat.Delete()
    builder = DiagramBuilder()
    station = builder.AddSystem(generate_environment())
    plant = station.GetSubsystemByName("plant")

    # point cloud cropbox points
    cropPointA = [-.28, -.72, 0.36]
    cropPointB = [0.26, -.47, 0.57]

    x_bin_grasp_selector = builder.AddSystem(
        grasp_selector.GraspSelector(plant,
                      #plant.GetModelInstanceByName("shelves1"),
                      plant.GetFrameByName("shelves1_frame"),
                      camera_body_indices=[
                          plant.GetBodyIndices(
                              plant.GetModelInstanceByName("camera0"))[0],
                          plant.GetBodyIndices(
                              plant.GetModelInstanceByName("camera1"))[0],
                          plant.GetBodyIndices(
                              plant.GetModelInstanceByName("camera2"))[0]
                      ], cropPointA=cropPointA, cropPointB=cropPointB, meshcat=meshcat, running_as_notebook=running_as_notebook))
    builder.Connect(station.GetOutputPort("camera0_point_cloud"),x_bin_grasp_selector.get_input_port(0))
    builder.Connect(station.GetOutputPort("camera1_point_cloud"), x_bin_grasp_selector.get_input_port(1))
    builder.Connect(station.GetOutputPort("camera2_point_cloud"), x_bin_grasp_selector.get_input_port(2))
    builder.Connect(station.GetOutputPort("body_poses"), x_bin_grasp_selector.GetInputPort("body_poses"))

    planner = builder.AddSystem(planner_class.Planner(plant, JOINT_COUNT, meshcat, rs, PREPICK_DISTANCE))
    builder.Connect(station.GetOutputPort("body_poses"), planner.GetInputPort("body_poses"))
    builder.Connect(x_bin_grasp_selector.get_output_port(), planner.GetInputPort("x_bin_grasp"))
    builder.Connect(station.GetOutputPort("wsg_state_measured"), planner.GetInputPort("wsg_state"))
    builder.Connect(station.GetOutputPort("iiwa_position_measured"), planner.GetInputPort("iiwa_position"))

    robot = station.GetSubsystemByName("iiwa_controller").get_multibody_plant_for_control()

    # Set up differential inverse kinematics.
    diff_ik = AddIiwaDifferentialIK(builder, robot)
    builder.Connect(planner.GetOutputPort("X_WG"), diff_ik.get_input_port(0))
    builder.Connect(station.GetOutputPort("iiwa_state_estimated"), diff_ik.GetInputPort("robot_state"))
    builder.Connect(planner.GetOutputPort("reset_diff_ik"), diff_ik.GetInputPort("use_robot_state"))

    builder.Connect(planner.GetOutputPort("wsg_position"), station.GetInputPort("wsg_position"))

    # The DiffIK and the direct position-control modes go through a PortSwitch
    switch = builder.AddSystem(PortSwitch(JOINT_COUNT))
    builder.Connect(diff_ik.get_output_port(), switch.DeclareInputPort("diff_ik"))
    builder.Connect(planner.GetOutputPort("iiwa_position_command"), switch.DeclareInputPort("position"))
    builder.Connect(switch.get_output_port(), station.GetInputPort("iiwa_position"))
    builder.Connect(planner.GetOutputPort("control_mode"), switch.get_port_selector_input_port())

    visualizer = MeshcatVisualizer.AddToBuilder(builder, station.GetOutputPort("query_object"), meshcat)    
    diagram = builder.Build()

    if SAVE_DIAGRAM:
        svg = SVG(pydot.graph_from_dot_data(diagram.GetGraphvizString())[0].create_svg())
        with open('diagram.svg', 'w') as f:
            f.write(svg.data)

    simulator = Simulator(diagram)
    context = simulator.get_context()

    plant_context = plant.GetMyMutableContextFromRoot(context)
    helpers.place_items(plant,plant_context, x=-0.20, y=-0.50, z=0.4)

    # run simulation
    simulator.AdvanceTo(0.1)
    meshcat.Flush()  # Wait for the large object meshes to get to meshcat.
    visualizer.StartRecording(True)
    meshcat.AddButton("Stop Simulation", "Escape")
    while not planner._simulation_done and simulator.get_context().get_time() < MAX_TIME and meshcat.GetButtonClicks("Stop Simulation") < 1:
        simulator.AdvanceTo(simulator.get_context().get_time() + 2.0)
    visualizer.PublishRecording()

clutter_clearing_demo()

while True: pass

