"""
Annealer class for a Double-DQN
"""

import copy
import math
import numpy as np
from collections import deque
import torch

from environment.state import CircuitStateDQN
from qroute.environment.env import step


class AnnealerDQN:
    """
    Class to perform simulated annealing using a value function approximator
    """

    def __init__(self, agent, device):
        """
        Sets hyper-parameters and stores the agent and environment to initialize Annealer

        :param agent: Agent, to evaluate the value function
        :param device: environment, maintaining the device and state
        """
        self.initial_temperature = 60.0
        self.min_temperature = 0.1
        self.cooling_multiplier = 0.95

        self.device = device
        self.agent = agent
        self.reversed_gates_deque = deque(maxlen=20)

    def get_neighbour_solution(self, current_solution, current_state: CircuitStateDQN):
        """
        Get a solution neighboring current, that is one swap inserted
        :param current_solution: list of edges to swap, current solution to start with
        :param current_state: State, the current state of mapping and progress
        :return: list, neighbor solution
        """
        neighbour_solution = copy.copy(current_solution)
        available_edges = current_state.swappable_edges(neighbour_solution)

        if available_edges is None or len(available_edges) == 0:
            raise RuntimeError("Ran out of edges to swap")

        edge_index_to_swap = np.random.choice(available_edges, 1)
        neighbour_solution[edge_index_to_swap] = (neighbour_solution[edge_index_to_swap] + 1) % 2

        self.check_valid_solution(neighbour_solution, current_state.protected_edges)

        return neighbour_solution

    def get_energy(self, solution, current_state=None, action_chooser='model'):
        """
        Returns the energy function (negative value function) for the current state using the model.
        :param solution: list of edges to swap as a boolean array
        :param current_state: State, the state at the current moment (q_locations, q_targets, protected_nodes, ...)
        :param action_chooser: str, if model, the current model is used to compute the value function,
                                    if target, then the target model is used.
        :return: int or float, the energy value
        """
        next_state_temp, _, _, _ = step(solution, current_state)
        q_val = self.agent(current_state, next_state_temp, action_chooser)
        return -q_val.detach()

    @staticmethod
    def acceptance_probability(current_energy, new_energy, temperature):
        """
        Compute acceptance probability given delta-energy

        :param current_energy: int/float, initial energy (negative of value function)
        :param new_energy: int/float, final energy (negative of value function)
        :param temperature: int/float, temperature in the simulation (randomness)
        :return: int or float, probability to accept
        """
        if new_energy < current_energy:
            return 1
        else:
            energy_diff = new_energy - current_energy
            probability = math.exp(-energy_diff/temperature)
            return probability

    def check_valid_solution(self, solution, forced_mask):
        """
        Checks if a solution is valid, i.e. does not use one node twice

        :param solution: list, boolean array of swaps, the solution to check
        :param forced_mask: list, blocking swaps which are not possible
        :raises: RuntimeError if the solution is invalid
        """
        for i in range(len(solution)):
            if forced_mask[i] and solution[i] == 1:
                raise RuntimeError('Solution is not safe: Protected edge is being swapped')

        if 1 in solution:
            swap_edge_indices = np.where(np.array(solution) == 1)[0]
            swap_edges = [self.device.edges[index] for index in swap_edge_indices]
            swap_nodes = [node for edge in swap_edges for node in edge]

            # return False if repeated swap nodes
            seen = set()
            for node in swap_nodes:
                if node in seen:
                    raise RuntimeError('Solution is not safe: Same node is being used twice in %s' % str(swap_edges))
                seen.add(node)

    def simulated_annealing(self, current_state, action_chooser='model', search_limit=None):
        """
        Uses Simulated Annealing to find the next best state based on combinatorial
        actions taken by the agent.

        :param current_state: State, the state before this iterations of sim-anneal
        :param action_chooser: str, if model, uses the model for value function
        :param search_limit: int, max iterations to search for
        :return: best_solution, value of best_energy
        """
        import qroute
        temp_state: qroute.environment.state.CircuitStateDQN = copy.copy(current_state)
        current_solution = self.generate_initial_solution(current_state)
        new_state: qroute.environment.state.CircuitStateDQN = copy.copy(current_state)
        assert temp_state == new_state, "State not preserved when selecting action"

        # FIXME: Never crosses this if condition, always stuck here, why is it not training?
        if np.all(current_solution == 0):
            # There are no actions possible often happens when only one gate is left, and it's already been scheduled
            if action_chooser == 'model':
                return current_solution, -np.inf
            else:
                return current_solution, 0

        temp = self.initial_temperature
        current_energy = self.get_energy(current_solution, current_state=current_state, action_chooser=action_chooser)
        best_solution = copy.copy(current_solution)
        best_energy = current_energy

        iterations_since_best = 0
        iterations = 0

        while temp > self.min_temperature:
            if search_limit is not None and iterations > search_limit:
                break

            new_solution = self.get_neighbour_solution(current_solution, current_state)
            new_energy = self.get_energy(new_solution, current_state=current_state, action_chooser=action_chooser)
            accept_prob = self.acceptance_probability(current_energy, new_energy, temp)

            if accept_prob > np.random.random():
                current_solution = new_solution
                current_energy = new_energy

                # Save best solution, so it can be returned if algorithm terminates at a sub-optimal solution
                if current_energy < best_energy:
                    best_solution = copy.copy(current_solution)
                    best_energy = current_energy
                    # intervals.append(iterations_since_best)
                    iterations_since_best = 0

            temp = temp * self.cooling_multiplier
            iterations_since_best += 1
            iterations += 1

        return best_solution, best_energy

    def generate_initial_solution(self, current_state: CircuitStateDQN):
        """
        Makes a random initial solution to start with by populating with whatever swaps possible

        :param current_state: State, the current state of mapping and progress
        :return: list, initial solution as boolean array of whether to swap each node
        """
        initial_solution = np.zeros(len(self.device.edges))
        available_edges = current_state.swappable_edges(initial_solution)
        if available_edges is None or len(available_edges) == 0:
            return initial_solution

        edge_index_to_swap = np.random.choice(available_edges)
        initial_solution[edge_index_to_swap] = (initial_solution[edge_index_to_swap] + 1) % 2
        return initial_solution

    def generate_forced_mask(self, protected_nodes):
        """
        Make a list of edges which are blocked given nodes which are blocked

        :param protected_nodes: list, nodes that are being user elsewhere
        :return: list, edges that are blocked
        """
        return list(map(lambda e: True if e[0] in protected_nodes or
                                          e[1] in protected_nodes else False, self.device.edges))

    @staticmethod
    def calculate_reversed_gates_proportion(suggestion, solution):
        """
        Calculates percentage of gates that are suggested but not really swapped in the solution

        :param suggestion: boolean array of gates that are suggested for swaps
        :param solution: boolean array of gates that are actually in the final solution
        :return: fraction of gates in suggestion not in solution
        """
        reversed_gates = [suggestion[i] == 1 and solution[i] == 0 for i in range(len(suggestion))]

        if sum(suggestion) == 0 or sum(reversed_gates) == 0:
            return 0.0

        return float(sum(reversed_gates)) / float(sum(suggestion))