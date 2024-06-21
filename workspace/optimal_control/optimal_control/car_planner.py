#!/usr/bin/python3

from dataclasses import asdict
from casadi import *

from optimal_control.utils import *
from optimal_control.problem import Problem

class CarPlanner(Problem):

    def __init__(self, robot : CarLikeRobot, number_of_waypoints = 6, granularity = 10, t_max = 40):
        super(CarPlanner, self).__init__()
        self.robot = robot
        self.prep_robot_information()

        # Formulation Parameters
        self.number_of_waypoints = number_of_waypoints
        self.granularity = granularity
        self.t_max = t_max # s

    def prep_robot_information(self):
        self.minimum_turning_radius = self.robot.get_minimum_turning_radius()
        self.wheel_base = self.robot.get_wheel_base()
        self.max_linear_velocity = self.robot.get_max_linear_velocity()
        self.max_acceleration = self.robot.get_max_acceleration()
        self.max_steering_angle = self.robot.get_max_steering_angle()

        self.k = 1/self.minimum_turning_radius
        self.v = self.max_linear_velocity*0.4

    def prep_problem(self, *args, **kwargs):
        self.waypoints : list[Waypoint] = []

        for i in range(self.number_of_waypoints):
            idx = str(i)

            # Make Waypoint
            waypoint = Waypoint(
                MX.sym("X"+idx, 4, 1),
                MX.sym("U"+idx, 2, 1),
                MX.sym("t"+idx, 1, 1)
            )

            # Declare Decision Variables
            for _, value in asdict(waypoint).items():
                self.set_variable(value.name(), value)

            self.waypoints.append(waypoint)

    def prep_constraints(self, *args, **kwargs):
        # Get Initial, Final waypoints, and Obstacles from arguments.
        self.initial_state : State = kwargs["init"]
        self.final_state : State = kwargs["final"]
        self.obstacles = kwargs["obstacles"]

        # The first state is X0 and the last is Xn
        X0 = self.waypoints[0]; Xn = self.waypoints[-1]

        # Boundary Conditions
        self.set_equality_constraint("x0", X0.x, self.initial_state.x)
        self.set_equality_constraint("y0", X0.y, self.initial_state.y)
        self.set_equality_constraint("th0", X0.theta, self.initial_state.theta)

        # Final Boundary Conditions are subject to change based on problem description
        self.set_equality_constraint("xn", Xn.x, self.final_state.x)
        self.set_equality_constraint("yn", Xn.y, self.final_state.y)
        self.set_equality_constraint("thn", Xn.theta, self.final_state.theta)

        # Iterate through all states to establish constraints
        time_sum = 0
        for i in range(1, self.number_of_waypoints):
            idx = str(i)

            # Current state and previous state (Xim1 = Xi-1)
            Xi = self.waypoints[i]; Xim1 = self.waypoints[i-1]

            # Velocity Constraint
            self.set_constraint("v"+idx, Xi.v, 0, self.v)

            # Curvature Constraint
            self.set_constraint("k"+idx, Xi.k, -self.k, self.k)

            # Time Constraint
            self.set_constraint("t"+idx, Xi.t, 0)

            # Start with establishing the equality constraint between final and initial positions
            dx = Xim1.x; dy = Xim1.y; dth = Xim1.theta
            dk = Xim1.k; dt = Xi.t*(1/self.granularity)

            for j in range(self.granularity + 1):
                jdx = str(j) + idx

                dk += Xi.s*dt
                dx += Xi.v*cos(dth)*dt
                dy += Xi.v*sin(dth)*dt
                dth += dk*Xi.v*dt

                # Kinematic Limits
                self.set_constraint("k"+jdx, dk, -self.k, self.k)

                # For Plotting
                self.set_constraint("intermediate_x"+jdx, dx)
                self.set_constraint("intermediate_y"+jdx, dy)
                self.set_constraint("intermediate_th"+jdx, dth)

                # Check for obstacles
                obs_id = 0
                for obstacle in self.obstacles:
                    odx = str(obs_id) + jdx
                    # Distance from center of the circle
                    check_obstacle = lambda x, y : \
                        power(obstacle[0] - x, 2) + power(obstacle[1] - y, 2) - power(obstacle[2]+obstacle[3], 2)
                    self.set_constraint("check_obs"+odx, check_obstacle(dx, dy), 0)
                    obs_id += 1

            # G2 Continuity Constraints
            self.set_equality_constraint("x"+idx, Xi.x - dx, 0)
            self.set_equality_constraint("y"+idx, Xi.y - dy, 0)
            self.set_equality_constraint("theta"+idx, Xi.theta - dth, 0)
            self.set_equality_constraint("k"+idx, Xi.k - dk, 0)

            time_sum += Xi.t

        self.set_constraint("time_sum", time_sum, 0, self.t_max)

    def objective(self, *args, **kwargs):
        # Distance between two states : Euclidean
        # TODO: Is this really how I should check path length?
        def length(stateA : State, stateB : State):
            dx = stateB.x - stateA.x
            dy = stateB.y - stateA.y
            dth = stateB.theta - stateA.theta
            return power(dx, 2) + power(dy, 2)

        path_length = 0
        for i in range(1, self.number_of_waypoints):
            path_length += length(self.waypoints[i], self.waypoints[i-1])

        return path_length

    def initial_guess(self, *args, **kwargs):
        dx = self.final_state.x - self.initial_state.x
        dy = self.final_state.y - self.initial_state.y

        slope = dy/dx

        guess_variables = []; first = True
        r = sqrt(power(dx, 2) + power(dy, 2))*0.5

        if abs(slope) < 1e-2:
            # A straight line guess
            k = 0
            distance = r
        else:
            # An arc like initial guess
            k = self.v/r
            distance = r*pi

        x = self.initial_state.x; y = self.initial_state.y; theta = self.initial_state.theta
        t = distance/self.v
        dt = t/self.number_of_waypoints

        for i in range(self.number_of_waypoints):
            guess_variables.append(x)
            guess_variables.append(y)
            guess_variables.append(theta)
            guess_variables.append(0) # Zero change in curvature
            guess_variables.append(dt) # Equal time for all states
            guess_variables.append(self.v) # Unit Speed along the path
            guess_variables.append(k)

            if not first:
                x += sign(dx)*self.v*cos(theta)*dt
                y += sign(dy)*self.v*sin(theta)*dt
                theta += self.v*k*dt
            else:
                first = False

        return vertcat(*guess_variables)

    def solve(self, *args, **kwargs):
        constraints = self.get_constraints()
        nlp = {
            "x" : vertcat(*self.get_variables()),
            "f": self.objective(),
            "g": vertcat(*constraints[0])
        }

        opts = {
            "ipopt": {
                "hessian_approximation": "limited-memory",
                "max_iter": 10000,
                # "max_cpu_time": 1.0,
                "fast_step_computation": "yes"
            },
            "jit": True,
            "compiler": "shell",
            "expand": True
        }

        sol = nlpsol("Solver", "ipopt", nlp, opts)

        if "warm_start" not in kwargs.keys():
            return sol(
                x0 = self.initial_guess(),
                lbg = vertcat(*constraints[1]),
                ubg = vertcat(*constraints[2])
            ), sol
        else:
            return sol(
                x0 = kwargs["warm_start"],
                lbg = vertcat(*constraints[1]),
                ubg = vertcat(*constraints[2])
            ), sol