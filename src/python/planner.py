from pydrake.all import (AbstractValue, InputPortIndex, LeafSystem,
                         PiecewisePolynomial, PiecewisePose, RigidTransform,
                         RollPitchYaw, Sphere, Rgba)
from manipulation.meshcat_utils import AddMeshcatTriad
from manipulation.pick import (MakeGripperCommandTrajectory, MakeGripperPoseTrajectory)
from copy import copy

import numpy as np

import planner_state
import pick
from kinematic import run_trajopt

from enum import Enum

class PlannerState(Enum):
    WAIT_FOR_OBJECTS_TO_SETTLE = 1
    PICKING_WITH_DIFFIK = 2
    PICKING_WITH_TRAJOPT = 3
    GO_HOME = 4

class Planner(LeafSystem):
    def __init__(self, plant, joint_count, meshcat, rs, prepick_distance):
        LeafSystem.__init__(self)
        self._gripper_body_index = plant.GetBodyByName("body").index()
        self.DeclareAbstractInputPort(
            "body_poses", AbstractValue.Make([RigidTransform()]))
        self._x_bin_grasp_index = self.DeclareAbstractInputPort(
            "x_bin_grasp", AbstractValue.Make(
                (np.inf, RigidTransform()))).get_index()
        self._wsg_state_index = self.DeclareVectorInputPort("wsg_state", 2).get_index()

        self._mode_index = self.DeclareAbstractState(
            AbstractValue.Make(PlannerState.WAIT_FOR_OBJECTS_TO_SETTLE))
        self._traj_X_G_index = self.DeclareAbstractState(
            AbstractValue.Make(PiecewisePose()))
        self._traj_wsg_index = self.DeclareAbstractState(
            AbstractValue.Make(PiecewisePolynomial()))
        self._X_G_frames_index = self.DeclareAbstractState(AbstractValue.Make({}))
        self._times_index = self.DeclareAbstractState(AbstractValue.Make(
            {"initial": 0.0}))
        self._attempts_index = self.DeclareDiscreteState(1)
        self._traj_q_place_index = self.DeclareAbstractState(AbstractValue.Make(PiecewisePolynomial()))

        self.DeclareAbstractOutputPort(
            "X_WG", lambda: AbstractValue.Make(RigidTransform()),
            self.CalcGripperPose)
        self.DeclareAbstractOutputPort(
            "X_WG_measured", lambda: AbstractValue.Make(RigidTransform()),
            self.CalcGripperPose)
        self.DeclareVectorOutputPort("wsg_position", 1, self.CalcWsgPosition)

        # For GoHome mode.
        num_positions = joint_count
        self._iiwa_position_index = self.DeclareVectorInputPort(
            "iiwa_position", num_positions).get_index()
        self.DeclareAbstractOutputPort(
            "control_mode", lambda: AbstractValue.Make(InputPortIndex(0)),
            self.CalcControlMode)
        self.DeclareAbstractOutputPort(
            "reset_diff_ik", lambda: AbstractValue.Make(False),
            self.CalcDiffIKReset)
        self._q0_index = self.DeclareDiscreteState(num_positions)  # for q0
        self._traj_q_index = self.DeclareAbstractState(
            AbstractValue.Make(PiecewisePolynomial()))
        self.DeclareVectorOutputPort("iiwa_position_command", num_positions,
                                     self.CalcIiwaPosition)
        self.DeclareInitializationDiscreteUpdateEvent(self.Initialize)

        self._simulation_done = False
        self.DeclarePeriodicUnrestrictedUpdateEvent(0.1, 0.0, self.Update)
        
        self.meshcat = meshcat
        self.rs = rs
        self.prepick_distance = prepick_distance

    def Update(self, context, state):
        if self._simulation_done:
            return

        mode = context.get_abstract_state(int(self._mode_index)).get_value()

        current_time = context.get_time()
        times = context.get_abstract_state(int(
            self._times_index)).get_value()

        if mode == PlannerState.WAIT_FOR_OBJECTS_TO_SETTLE:
            if context.get_time() - times["initial"] > 1.0:
                self.Plan(context, state)
            return

        elif mode == PlannerState.GO_HOME:
            traj_q = context.get_mutable_abstract_state(int(
                self._traj_q_index)).get_value()
            if not traj_q.is_time_in_range(current_time):
                self.Plan(context, state)
            return

        elif mode == PlannerState.PICKING_WITH_TRAJOPT:
            traj_q = context.get_mutable_abstract_state(int(
                self._traj_q_index)).get_value()
            if not traj_q.is_time_in_range(current_time):
                # switch to diff ik to pick or place
                state.get_mutable_abstract_state(int(
                    self._mode_index)).set_value(
                        PlannerState.PICKING_WITH_DIFFIK)
                print("mode = PICKING_WITH_DIFFIK")

                return

        # mode == PICKING_WITH_TRAJOPT or PICKING_WITH_DIFFIK

        if (current_time > times["postpick"] and
                current_time < times["preplace"]):
            
            # switch to traj opt to carry the object to the bin
            if mode == PlannerState.PICKING_WITH_DIFFIK:
                state.get_mutable_abstract_state(int(self._mode_index)).set_value(PlannerState.PICKING_WITH_TRAJOPT)
                print("mode = PICKING_WITH_TRAJOPT")
                X_G = context.get_abstract_state(int(
                    self._X_G_frames_index)).get_value()
                
                # # run trajectory optimization from initial to prepick
                # duration = times["preplace"] - times["postpick"]
                # q_traj = run_trajopt(X_G["postpick"], X_G["preplace"], start_time=times["postpick"], duration=duration)
                # state.get_mutable_abstract_state(int(
                #     self._traj_q_index)).set_value(q_traj)
                q_traj = context.get_mutable_abstract_state(int(
                    self._traj_q_place_index)).get_value()
                state.get_mutable_abstract_state(int(
                     self._traj_q_index)).set_value(q_traj)
                return

            # If we are between pick and place and the gripper is closed, then
            # we've missed or dropped the object.  Time to replan.
            wsg_state = self.get_input_port(self._wsg_state_index).Eval(context)
            if wsg_state[0] < 0.01:
                attempts = state.get_mutable_discrete_state(
                    int(self._attempts_index)).get_mutable_value()
                
                # exit
                if attempts[0] > 5:
                    attempts[0] = 0
                    return
                
                attempts[0] += 1
                state.get_mutable_abstract_state(int(
                    self._mode_index)).set_value(
                        PlannerState.WAIT_FOR_OBJECTS_TO_SETTLE)
                print("mode = WAIT_FOR_OBJECTS_TO_SETTLE")
                times = {"initial": current_time}
                state.get_mutable_abstract_state(int(
                    self._X_G_frames_index)).set_value({})
                state.get_mutable_abstract_state(int(
                    self._times_index)).set_value(times)
                X_G = self.get_input_port(0).Eval(context)[int(
                    self._gripper_body_index)]
                state.get_mutable_abstract_state(int(
                    self._traj_X_G_index)).set_value(PiecewisePose.MakeLinear([current_time, np.inf], [X_G, X_G]))
                return

        traj_X_G = context.get_abstract_state(int(
            self._traj_X_G_index)).get_value()
        # plan to pick the next object
        if not traj_X_G.is_time_in_range(current_time):
            self.Plan(context, state)
            return

        X_G = self.get_input_port(0).Eval(context)[int(self._gripper_body_index)]
        # stop and replan
        if mode == PlannerState.PICKING_WITH_DIFFIK and np.linalg.norm(
            traj_X_G.GetPose(current_time).translation() - X_G.translation()) > 0.2:
            self.GoHome(context, state)

    def GoHome(self, context, state):
        print("Replanning due to large tracking error.")
        state.get_mutable_abstract_state(int(
            self._mode_index)).set_value(
                PlannerState.GO_HOME)
        print("mode = GO_HOME")
        q = self.get_input_port(self._iiwa_position_index).Eval(context)
        q0 = copy(context.get_discrete_state(self._q0_index).get_value())
        q0[0] = q[0]  # Safer to not reset the first joint.

        current_time = context.get_time()
        q_traj = PiecewisePolynomial.FirstOrderHold(
            [current_time, current_time + 5.0], np.vstack((q, q0)).T)
        state.get_mutable_abstract_state(int(
            self._traj_q_index)).set_value(q_traj)


    def Plan(self, context, state):
        mode = copy(
            state.get_mutable_abstract_state(int(self._mode_index)).get_value())

        X_G = {
            "initial":
                self.get_input_port(0).Eval(context)
                [int(self._gripper_body_index)]
        }

        # pick pose calculation
        cost = np.inf
        mode = PlannerState.PICKING_WITH_TRAJOPT
        for i in range(5):
            cost, X_G["pick"] = self.get_input_port(
                self._x_bin_grasp_index).Eval(context)
                        
            if not np.isinf(cost):
                break
        
        if np.isinf(cost):
            self._simulation_done = True
            print("Could not find a valid grasp in either bin after 5 attempts")
            return
        state.get_mutable_abstract_state(int(self._mode_index)).set_value(mode)
        print("mode = PICKING_WITH_TRAJOPT")

        # place pose calculation
        x_range = [.35, .65]
        y_range = [0, .35]

        # Place in the bin:
        X_G["place"] = RigidTransform(
            RollPitchYaw(-np.pi / 2, 0, np.pi / 2),
            [self.rs.uniform(x_range[0], x_range[1]),
                self.rs.uniform(y_range[0], y_range[1]),
                0])
            
        self.meshcat.SetObject("place1", Sphere(0.02), rgba=Rgba(.9, .1, .1, 1))
        self.meshcat.SetTransform("place1", RigidTransform([x_range[0], y_range[0], 0]))
        self.meshcat.SetObject("place2", Sphere(0.02), rgba=Rgba(.1, .9, .1, 1))
        self.meshcat.SetTransform("place2", RigidTransform([x_range[1], y_range[1], 0]))

        # plan trajectory
        X_G, times = pick.MakeGripperFrames(X_G, t0=context.get_time(), prepick_distance=self.prepick_distance)

        # run trajectory optimization from initial to prepick
        #duration = times["prepick"] - times["initial"]
        q_traj_pick = run_trajopt(X_G["initial"], X_G["prepick"], start_time=times["initial"])
        state.get_mutable_abstract_state(int(
            self._traj_q_index)).set_value(q_traj_pick)
        pick_duration = q_traj_pick.end_time() - q_traj_pick.start_time()

        q_traj_place = run_trajopt(X_G["postpick"], X_G["preplace"], start_time=times["postpick"])
        state.get_mutable_abstract_state(int(
            self._traj_q_place_index)).set_value(q_traj_place)
        place_duration = q_traj_place.end_time() - q_traj_place.start_time()
        
        times = pick.RecomputeTimes(times, pick_duration, place_duration)

        print(
            f"t={int(context.get_time())}s - Planned {int(times['postplace'] - times['initial'])} seconds trajectory for picking from the shelf."
        )
        state.get_mutable_abstract_state(int(
            self._X_G_frames_index)).set_value(X_G)
        state.get_mutable_abstract_state(int(
            self._times_index)).set_value(times)


        if True:  # Useful for debugging
            #AddMeshcatTriad(meshcat, "X_Oinitial", X_PT=X_O["initial"])
            #AddMeshcatTriad(meshcat, "X_Gpickwrotat", X_PT=X_G["pick"] @ RigidTransform(RotationMatrix.MakeXRotation(np.pi / 10)))
            AddMeshcatTriad(self.meshcat, "X_Gprepick", X_PT=X_G["prepick"])
            AddMeshcatTriad(self.meshcat, "X_Gpick", X_PT=X_G["pick"])
            AddMeshcatTriad(self.meshcat, "X_Gplace", X_PT=X_G["place"])

        traj_X_G = MakeGripperPoseTrajectory(X_G, times)
        traj_wsg_command = MakeGripperCommandTrajectory(times)
        
        AddMeshcatTriad(self.meshcat, "X_Ginitial", X_PT=X_G["initial"])
        
        state.get_mutable_abstract_state(int(
            self._traj_X_G_index)).set_value(traj_X_G)
        state.get_mutable_abstract_state(int(
            self._traj_wsg_index)).set_value(traj_wsg_command)

    def start_time(self, context):
        return context.get_abstract_state(
            int(self._traj_X_G_index)).get_value().start_time()

    def end_time(self, context):
        return context.get_abstract_state(
            int(self._traj_X_G_index)).get_value().end_time()

    def CalcGripperPose(self, context, output):
        mode = context.get_abstract_state(int(self._mode_index)).get_value()

        traj_X_G = context.get_abstract_state(int(
            self._traj_X_G_index)).get_value()
        if (traj_X_G.get_number_of_segments() > 0 and
                traj_X_G.is_time_in_range(context.get_time())):
            # Evaluate the trajectory at the current time, and write it to the
            # output port.
            output.set_value(
                context.get_abstract_state(int(
                    self._traj_X_G_index)).get_value().GetPose(
                        context.get_time()))
            return

        # Command the current position (note: this is not particularly good if the velocity is non-zero)
        output.set_value(self.get_input_port(0).Eval(context)
            [int(self._gripper_body_index)])
    
    def MeasureGripperPose(self, context, output):
        output.set_value(self.get_input_port(0).Eval(context)
            [int(self._gripper_body_index)])

    def CalcWsgPosition(self, context, output):
        mode = context.get_abstract_state(int(self._mode_index)).get_value()
        opened = np.array([0.107])
        closed = np.array([0.0])

        if mode == PlannerState.GO_HOME:
            # Command the open position
            output.SetFromVector([opened])
            return

        traj_wsg = context.get_abstract_state(int(
            self._traj_wsg_index)).get_value()
        if (traj_wsg.get_number_of_segments() > 0 and
                traj_wsg.is_time_in_range(context.get_time())):
            # Evaluate the trajectory at the current time, and write it to the
            # output port.
            output.SetFromVector(traj_wsg.value(context.get_time()))
            return

        # Command the open position
        output.SetFromVector([opened])

    def CalcControlMode(self, context, output):
        mode = context.get_abstract_state(int(self._mode_index)).get_value()

        if mode == PlannerState.GO_HOME or mode == PlannerState.PICKING_WITH_TRAJOPT:
            output.set_value(InputPortIndex(2))  # Go Home
        else:
            output.set_value(InputPortIndex(1))  # Diff IK

    def CalcDiffIKReset(self, context, output):
        mode = context.get_abstract_state(int(self._mode_index)).get_value()
        
        if mode == PlannerState.GO_HOME or mode == PlannerState.PICKING_WITH_TRAJOPT:
            output.set_value(True)
        else:
            output.set_value(False)

    def Initialize(self, context, discrete_state):
        discrete_state.set_value(
            int(self._q0_index),
            self.get_input_port(int(self._iiwa_position_index)).Eval(context))

    def CalcIiwaPosition(self, context, output):
        traj_q = context.get_mutable_abstract_state(int(
                self._traj_q_index)).get_value()

        output.SetFromVector(traj_q.value(context.get_time()))