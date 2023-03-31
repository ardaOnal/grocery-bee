import time

import os
import numpy as np
from pydrake.all import (AddMultibodyPlantSceneGraph, BsplineTrajectory,
                         DiagramBuilder, KinematicTrajectoryOptimization,
                         MeshcatVisualizer, MeshcatVisualizerParams,
                         MinimumDistanceConstraint, Parser, PositionConstraint,
                         Rgba, RigidTransform, Role, Solve, Sphere,
                         StartMeshcat, JointIndex, Simulator, RotationMatrix, PiecewisePolynomial, PidController, TrajectorySource,
                         InverseDynamicsController, Multiplexer, SchunkWsgPositionController, MakeMultibodyStateToWsgStateSystem, ModelInstanceIndex)

import pydrake.systems.framework
from pydrake.systems.primitives import ConstantVectorSource

from manipulation.meshcat_utils import PublishPositionTrajectory
from manipulation.scenarios import AddIiwa, AddShape, AddWsg, AddPackagePaths, MakeManipulationStation
from manipulation.utils import FindResource

# Start the visualizer.
meshcat = StartMeshcat()

def create_system_with_station():
    model_directives = """
    directives:
    - add_directives:
        file: package://grocery/kinematic.dmd.yaml
    - add_directives:
        file: package://grocery/iiwa_and_wsg_with_collision.dmd.yaml    
    - add_model:
        name: foam_brick
        file: package://drake/examples/manipulation_station/models/061_foam_brick.sdf
        # file: package://grocery/meshes/005_tomato_soup_can.sdf 
    """
    diagram = MakeManipulationStation(model_directives, time_step=0.001,
                                    package_xmls=[os.path.join(os.path.dirname(
                                       os.path.realpath(__file__)), "models/package.xml")])
    return diagram


def create_system():
    builder = DiagramBuilder()
    plant, scene_graph = AddMultibodyPlantSceneGraph(builder, time_step=0.001)
    iiwa = AddIiwa(plant, collision_model="with_box_collision")
    wsg = AddWsg(plant, iiwa, welded=True, sphere=True)

    parser = Parser(plant)
    AddPackagePaths(parser)
    parser.package_map().AddPackageXml(os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "models/package.xml"))
    parser.AddModels(
        "src/python/models/kinematic.dmd.yaml")

    return builder, parser, plant, scene_graph, iiwa, wsg


def make_internal_model():
    builder, parser, plant, scene_graph, iiwa, wsg = create_system()
    plant.Finalize()
    return builder.Build()


def MakeGripperCommandTrajectory(times):
    """Constructs a WSG command trajectory from the plan "sketch"."""
    opened = np.array([0.107])
    closed = np.array([0.0])

    traj_wsg_command = PiecewisePolynomial.FirstOrderHold(
        [0.0, 0.4], np.hstack([[opened],[opened]]))
    traj_wsg_command.AppendFirstOrderSegment(0.7, closed)
    traj_wsg_command.AppendFirstOrderSegment(1.8, closed)
    traj_wsg_command.AppendFirstOrderSegment(2.1, opened)
    traj_wsg_command.AppendFirstOrderSegment(2.5, opened)
    return traj_wsg_command


def trajopt(plant, context, X_WStart, X_WGoal):
    plant_context = plant.GetMyContextFromRoot(context)

    num_q = plant.num_positions()
    q0 = plant.GetPositions(plant_context)
    wsg = plant.GetModelInstanceByName("gripper")
    gripper_frame = plant.GetFrameByName("body", wsg)

    trajopt = KinematicTrajectoryOptimization(plant.num_positions(), 10)
    prog = trajopt.get_mutable_prog()
    trajopt.AddDurationCost(1.0)
    trajopt.AddPathLengthCost(1.0)
    trajopt.AddPositionBounds(plant.GetPositionLowerLimits(),
                              plant.GetPositionUpperLimits())
    trajopt.AddVelocityBounds(plant.GetVelocityLowerLimits(),
                              plant.GetVelocityUpperLimits())

    trajopt.AddDurationConstraint(.5, 50)

    # start constraint
    start_constraint = PositionConstraint(plant, plant.world_frame(),
                                          X_WStart.translation(),
                                          X_WStart.translation(), gripper_frame,
                                          [0, 0.1, 0], plant_context)
    trajopt.AddPathPositionConstraint(start_constraint, 0)
    prog.AddQuadraticErrorCost(np.eye(num_q), q0,
                               trajopt.control_points()[:, 0])

    # goal constraint
    goal_constraint = PositionConstraint(plant, plant.world_frame(),
                                         X_WGoal.translation(),
                                         X_WGoal.translation(), gripper_frame,
                                         [0, 0.1, 0], plant_context)
    trajopt.AddPathPositionConstraint(goal_constraint, 1)
    prog.AddQuadraticErrorCost(np.eye(num_q), q0,
                               trajopt.control_points()[:, -1])

    # start and end with zero velocity
    trajopt.AddPathVelocityConstraint(np.zeros((num_q, 1)), np.zeros(
        (num_q, 1)), 0)
    trajopt.AddPathVelocityConstraint(np.zeros((num_q, 1)), np.zeros(
        (num_q, 1)), 1)

    # Solve once without the collisions and set that as the initial guess for
    # the version with collisions.
    result = Solve(prog)
    if not result.is_success():
        print("Trajectory optimization failed, even without collisions!")
        print(result.get_solver_id().name())
    trajopt.SetInitialGuess(trajopt.ReconstructTrajectory(result))

    # collision constraints
    collision_constraint = MinimumDistanceConstraint(plant, 0.001,
                                                     plant_context, None, 0.01)
    evaluate_at_s = np.linspace(0, 1, 50)
    for s in evaluate_at_s:
        trajopt.AddPathPositionConstraint(collision_constraint, s)

    def PlotPath(control_points):
        traj = BsplineTrajectory(trajopt.basis(),
                                 control_points.reshape((3, -1)))
        meshcat.SetLine('positions_path',
                        traj.vector_values(np.linspace(0, 1, 50)))

    # prog.AddVisualizationCallback(PlotPath,
    #                              trajopt.control_points().reshape((-1,)))
    result = Solve(prog)
    if not result.is_success():
        print("Trajectory optimization failed")
        print(result.get_solver_id().name())

    return trajopt.ReconstructTrajectory(result)


def trajopt_shelves_demo():
    meshcat.Delete()
    builder = DiagramBuilder()

    # set start & goal
    #X_WStart = RigidTransform([0, -0.8, 0.59])  # top
    X_WStart = RigidTransform([0, -0.8, 0.35])  # middle
    #X_WStart = RigidTransform([0, -0.8, 0.07])  # bottom
    #meshcat.SetObject("start", Sphere(0.02), rgba=Rgba(.9, .1, .1, 1))
    #meshcat.SetTransform("start", X_WStart)
    X_WGoal = RigidTransform([0.6, 0.1, 0.1])
    #meshcat.SetObject("goal", Sphere(0.02), rgba=Rgba(.1, .9, .1, 1))
    #meshcat.SetTransform("goal", X_WGoal)

    # create actual system
    #builder, parser, plant, scene_graph, iiwa, wsg = create_system()
    #parser.AddModelFromFile("src/python/models/meshes/005_tomato_soup_can.sdf")
    #plant.SetDefaultFreeBodyPose(plant.GetBodyByName("base_link_soup"),
    #                             X_WStart @ RigidTransform(RotationMatrix.MakeXRotation(np.pi / 2)))
    #plant.Finalize()
    station = builder.AddSystem(create_system_with_station())
    plant = station.GetSubsystemByName("plant")
    iiwa = plant.GetModelInstanceByName("iiwa")
    wsg = plant.GetModelInstanceByName("wsg")
    plant.SetDefaultFreeBodyPose(plant.GetBodyByName("base_link"),
                                X_WStart @ RigidTransform(RotationMatrix.MakeXRotation(np.pi / 2)))
    plant.SetDefaultPositions(wsg, [-0.055, 0.055])

    # create an internal model
    internal_model = make_internal_model()
    internal_context = internal_model.CreateDefaultContext()
    internal_plant = internal_model.GetSubsystemByName("plant")
    #internal_plant = station.GetSubsystemByName(
    #    "iiwa_controller").get_multibody_plant_for_control()
    #internal_context = internal_plant.CreateDefaultContext()

    # trajectory optimization
    trajectory = trajopt(internal_plant, internal_context, X_WStart, X_WGoal)
    plant.SetDefaultPositions(iiwa, trajectory.value(0))
    internal_plant.SetDefaultPositions(trajectory.value(0))

    # create a discrete trajectory of joint positions and velocities
    times = np.append(np.arange(trajectory.start_time(), trajectory.end_time(), 0.001),
                      trajectory.end_time())
    pos = [trajectory.value(trajectory.start_time()) for i in range(1000)]
    pos += [trajectory.value(t) for t in times]
    pos += [trajectory.value(trajectory.end_time()) for i in range(1000)]
    position_stack = np.column_stack(pos)
    p_traj = PiecewisePolynomial.FirstOrderHold(np.arange(trajectory.start_time(), trajectory.end_time() + 2.001, 0.001), position_stack)
    v_traj = p_traj.derivative()

    p_source = builder.AddSystem(TrajectorySource(p_traj))
    builder.Connect(p_source.get_output_port(), station.GetInputPort("iiwa_position"))
    # # add a multiplexer to concat the desired position and velocity inputs
    # p_source = builder.AddSystem(TrajectorySource(p_traj))
    # v_source = builder.AddSystem(TrajectorySource(v_traj))
    # source = builder.AddSystem(
    #     Multiplexer([7, 7]))
    # builder.Connect(p_source.get_output_port(),
    #                 source.get_input_port(0))
    # builder.Connect(v_source.get_output_port(),
    #                 source.get_input_port(1))

    # # create a controller to track the trajectory
    # num_iiwa_positions = plant.num_positions(iiwa)
    # id_controller = builder.AddSystem(
    #     InverseDynamicsController(internal_plant,
    #                               kp=[100] * num_iiwa_positions,
    #                               ki=[1] * num_iiwa_positions,
    #                               kd=[20] * num_iiwa_positions,
    #                               has_reference_acceleration=False))
    # builder.Connect(station.GetOutputPort("iiwa_state_estimated"),
    #                 id_controller.get_input_port_estimated_state())
    # builder.Connect(source.get_output_port(),
    #                 id_controller.get_input_port_desired_state())
    # builder.Connect(id_controller.get_output_port(),
    #                 plant.get_actuation_input_port(iiwa))
    
    # Wsg controller.
    traj_wsg_command = MakeGripperCommandTrajectory(times)
    wsg_source = builder.AddSystem(TrajectorySource(traj_wsg_command))
    wsg_source.set_name("wsg_command")
    builder.Connect(wsg_source.get_output_port(),
                    station.GetInputPort("wsg_position"))

    visualizer = MeshcatVisualizer.AddToBuilder(
        builder, station.GetOutputPort("query_object"), meshcat,
        MeshcatVisualizerParams(role=Role.kIllustration))

    # set up the simulator and run the simulation.
    diagram = builder.Build()
    simulator = Simulator(diagram)
    visualizer.StartRecording(False)
    simulator.AdvanceTo(trajectory.end_time() + 2)
    visualizer.PublishRecording()


trajopt_shelves_demo()

while True:
    pass
