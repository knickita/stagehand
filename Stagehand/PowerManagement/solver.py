from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class PowerInputNode:
    position: tuple
    consumption: int = 0
    label: str = ""


class PowerSolverError(RuntimeError):
    pass


def position_key(position):
    return tuple(round(float(value), 6) for value in position)


def _worker_count(item_count):
    if item_count <= 1:
        return 1
    cpu_count = os.cpu_count() or 1
    return max(1, min(cpu_count, item_count))


def _run_parallel(func, inputs):
    inputs = list(inputs)
    if len(inputs) <= 1:
        return [func(item) for item in inputs]

    with ThreadPoolExecutor(max_workers=_worker_count(len(inputs))) as executor:
        return list(executor.map(func, inputs))


class PowerSolver:
    def __init__(self, max_power_for_line=3000, delta_excess=10):
        self.nodes = []
        self.node_lookup = {}
        self.edges_index = []
        self.edges = []
        self.power_nodes = []
        self.power_node_consumptions = {}
        self.forward_distances = []
        self.start_node = 0
        self.max_power_for_line = int(max_power_for_line)
        self.delta_excess = int(delta_excess)

        self.steiner_parents = {}
        self.steiner_children = defaultdict(set)
        self.steiner_junctions = set()
        self.steiner_paths = defaultdict(set)
        self.steiner_nodes = set()

        self.power_lines_consumption = {}
        self.power_lines_path = defaultdict(set)
        self.lines_patch = defaultdict(set)
        self.power_lines_per_node = {}

    def construct_indices(self, edge_positions, power_nodes, starting_node_position):
        graph = {}
        node_index = {}
        actual_index = 0

        for edge_start, edge_end in edge_positions:
            key_start = position_key(edge_start)
            key_end = position_key(edge_end)

            if key_start not in graph:
                graph[key_start] = []
                node_index[key_start] = actual_index
                actual_index += 1
            if key_end not in graph:
                graph[key_end] = []
                node_index[key_end] = actual_index
                actual_index += 1

            if key_start == key_end:
                continue

            graph[key_start].append(key_end)
            graph[key_end].append(key_start)

        if not graph:
            raise PowerSolverError("The power graph is empty.")

        self.nodes = [None] * actual_index
        self.edges_index = [0] * (actual_index + 1)
        self.edges = []

        for key, adjacent_keys in graph.items():
            node_id = node_index[key]
            self.nodes[node_id] = key
            self.edges_index[node_id] = len(self.edges)
            for adjacent_key in adjacent_keys:
                self.edges.append(node_index[adjacent_key])

        self.edges_index[actual_index] = len(self.edges)
        self.node_lookup = dict(node_index)

        self.power_nodes = []
        self.power_node_consumptions = {}
        for power_node in power_nodes:
            key = position_key(power_node.position)
            if key not in node_index:
                raise PowerSolverError(f"Power node is not present in graph: {power_node.label or key}")
            power_node_index = node_index[key]
            self.power_nodes.append(power_node_index)
            self.power_node_consumptions[power_node_index] = int(power_node.consumption)

        start_key = position_key(starting_node_position)
        if start_key not in node_index:
            raise PowerSolverError("Starting power node is not present in graph.")
        self.start_node = node_index[start_key]

    def solve(self):
        if not self.nodes:
            return

        self.forward_distances = self._forward_distances()
        possible_routes_graph = [set() for _node in self.power_nodes]

        route_inputs = [
            (index, power_node)
            for index, power_node in enumerate(self.power_nodes)
            if self.forward_distances[power_node] != 0
        ]
        route_results = _run_parallel(self._backward_route, route_inputs)

        used_node_counts = [0] * len(self.nodes)
        for route_index, used_nodes, possible_routes in route_results:
            possible_routes_graph[route_index] = possible_routes
            for node in used_nodes:
                used_node_counts[node] += 1

        graph = set()
        for possible_routes in possible_routes_graph:
            graph.update(possible_routes)

        used_node_counts[self.start_node] += 1
        steiner_edge_sets = _run_parallel(
            lambda power_node: self._layout_cables(power_node, graph, used_node_counts),
            self.power_nodes,
        )

        parallel_steiner_edges = set()
        for edge_set in steiner_edge_sets:
            parallel_steiner_edges.update(edge_set)

        self._compute_steiner_tree(parallel_steiner_edges)
        self._calculate_steiner_paths()
        self._optimize_steiner_tree()
        self._subdivide_power_lines()
        self._populate_power_lines_per_node()

    def _forward_distances(self):
        distances = [0] * len(self.nodes)
        visited = set()
        to_update = {self.start_node}
        distance = 0

        while to_update:
            temp = list(to_update)
            to_update.clear()

            for actual_node in temp:
                distances[actual_node] = distance
                visited.add(actual_node)

            for actual_node in temp:
                start = self.edges_index[actual_node]
                end = self.edges_index[actual_node + 1]
                for edge_index in range(start, end):
                    child = self.edges[edge_index]
                    if child not in visited:
                        to_update.add(child)

            distance += 1

        return distances

    def _backward_route(self, route_input):
        route_index, power_node = route_input
        used_nodes = set()
        possible_routes = set()
        to_be_explored = {power_node}

        while to_be_explored:
            temp = list(to_be_explored)
            to_be_explored.clear()

            for actual_node in temp:
                used_nodes.add(actual_node)

            for actual_node in temp:
                start = self.edges_index[actual_node]
                end = self.edges_index[actual_node + 1]
                min_nodes = []
                min_distance = self.forward_distances[actual_node]

                for edge_index in range(start, end):
                    child = self.edges[edge_index]
                    child_distance = self.forward_distances[child]
                    if child_distance < min_distance:
                        min_distance = child_distance
                        min_nodes = [child]
                    elif child_distance == min_distance:
                        min_nodes.append(child)

                for node in min_nodes:
                    to_be_explored.add(node)
                    possible_routes.add((node, actual_node))

        return route_index, used_nodes, possible_routes

    def _layout_cables(self, initial_power_node, graph, number_of_lines_for_node):
        start_node = initial_power_node
        number_of_lines = 0
        steiner_edges = set()
        explored_nodes = [2**31 - 1] * len(self.edges_index)

        while True:
            to_be_explored = []
            start = self.edges_index[start_node]
            end = self.edges_index[start_node + 1]
            for edge_index in range(start, end):
                child = self.edges[edge_index]
                edge = (child, start_node)
                if edge in graph:
                    if number_of_lines_for_node[child] < number_of_lines:
                        continue
                    if number_of_lines_for_node[child] > number_of_lines:
                        to_be_explored = []
                        number_of_lines = number_of_lines_for_node[child]
                    to_be_explored.append(edge)

            if not to_be_explored:
                return steiner_edges

            if len(to_be_explored) == 1:
                steiner_edges.add(to_be_explored[0])
                start_node = to_be_explored[0][0]
                continue

            explored_nodes[start_node] = 0
            actual_step = 0
            to_be_sub_explored = {start_node}
            found = False

            while not found:
                if not to_be_sub_explored:
                    break

                actual_step += 1
                temp = list(to_be_sub_explored)
                to_be_sub_explored.clear()

                for actual_node in temp:
                    start = self.edges_index[actual_node]
                    end = self.edges_index[actual_node + 1]
                    for edge_index in range(start, end):
                        child = self.edges[edge_index]
                        edge = (child, actual_node)
                        if edge in graph:
                            if number_of_lines_for_node[child] < number_of_lines:
                                continue
                            if number_of_lines_for_node[child] > number_of_lines:
                                found = True
                            to_be_sub_explored.add(child)
                            explored_nodes[child] = actual_step

            if not found:
                return steiner_edges

            max_node = -1
            max_lines = 0
            for node in to_be_sub_explored:
                if number_of_lines_for_node[node] > max_lines:
                    max_lines = number_of_lines_for_node[node]
                    max_node = node

            step = explored_nodes[max_node]
            backtrack_actual_node = max_node
            while step > 0:
                start = self.edges_index[backtrack_actual_node]
                end = self.edges_index[backtrack_actual_node + 1]
                for edge_index in range(start, end):
                    child = self.edges[edge_index]
                    if explored_nodes[child] == step - 1:
                        steiner_edges.add((backtrack_actual_node, child))
                        backtrack_actual_node = child
                        step -= 1
                        break

            start_node = max_node

    def _compute_steiner_tree(self, steiner_edges):
        self.steiner_parents = {}
        self.steiner_children = defaultdict(set)
        self.steiner_junctions = set()
        self.steiner_nodes = set()
        steiner_nodes_count = set()

        for parent, child in steiner_edges:
            self.steiner_parents[child] = parent
            self.steiner_children[parent].add(child)
            self.steiner_nodes.add(child)

            if parent not in steiner_nodes_count:
                steiner_nodes_count.add(parent)
            else:
                self.steiner_junctions.add(parent)

        self.steiner_nodes.add(self.start_node)

    def _calculate_steiner_paths(self):
        self.steiner_paths = defaultdict(set)
        steiner_nodes = list(self.steiner_junctions)
        steiner_nodes.extend(self.power_nodes)

        for node in steiner_nodes:
            temp_node = node
            while temp_node in self.steiner_parents:
                temp_node = self.steiner_parents[temp_node]
                if len(self.steiner_children.get(temp_node, ())) > 1:
                    break

            if temp_node == node:
                continue

            self.steiner_paths[temp_node].add(node)

    def _optimize_steiner_tree(self):
        to_check = {
            (parent, child)
            for parent, children in self.steiner_paths.items()
            for child in children
        }

        while True:
            to_check_array = list(to_check)
            find_inputs = [
                (index, parent, child)
                for index, (parent, child) in enumerate(to_check_array)
            ]
            best_results = _run_parallel(self._find_best_path, find_inputs)

            to_check.clear()
            best_length_improvement = 0
            best_index = -1
            best_paths = [[] for _item in to_check_array]
            length_improvements = [0] * len(to_check_array)

            for index, best_path, length_improvement in best_results:
                best_paths[index] = best_path
                length_improvements[index] = length_improvement
                if best_path:
                    to_check.add(to_check_array[index])
                    if length_improvement > best_length_improvement:
                        best_length_improvement = length_improvement
                        best_index = index

            if best_index == -1:
                break

            target_parent, target_child = to_check_array[best_index]
            node = target_child
            parent = self.steiner_parents[node]
            while parent != target_parent:
                self.steiner_nodes.discard(parent)
                self._remove_steiner_link(parent, node, to_check)
                node = parent
                parent = self.steiner_parents[node]
            self._remove_steiner_link(parent, node, to_check)

            best_path = best_paths[best_index]
            for index in range(len(best_path) - 1):
                parent = best_path[index]
                child = best_path[index + 1]
                self._add_steiner_link(parent, child, to_check)

            self.steiner_paths[target_parent].discard(target_child)
            if not self.steiner_paths[target_parent]:
                del self.steiner_paths[target_parent]
            self.steiner_paths[best_path[0]].add(target_child)

    def _remove_steiner_link(self, parent, child, to_check):
        if child in self.steiner_parents and self.steiner_parents[child] == parent:
            del self.steiner_parents[child]

        if parent in self.steiner_children:
            self.steiner_children[parent].discard(child)
            if not self.steiner_children[parent]:
                del self.steiner_children[parent]

        if parent in self.steiner_junctions and len(self.steiner_children.get(parent, ())) <= 1:
            self.steiner_junctions.discard(parent)

            pp = parent
            while pp not in self.steiner_junctions:
                if pp not in self.steiner_parents:
                    break
                pp = self.steiner_parents[pp]

            cc = parent
            while cc not in self.steiner_junctions:
                if cc not in self.steiner_children:
                    break
                cc = next(iter(self.steiner_children[cc]))

            self.steiner_paths[pp].discard(parent)
            if not self.steiner_paths[pp]:
                self.steiner_paths.pop(pp, None)
            self.steiner_paths[parent].discard(cc)
            if not self.steiner_paths[parent]:
                self.steiner_paths.pop(parent, None)
            self.steiner_paths[pp].add(cc)

            to_check.add((pp, cc))
            to_check.discard((pp, parent))
            to_check.discard((parent, cc))

    def _add_steiner_link(self, parent, child, to_check):
        self.steiner_parents[child] = parent
        self.steiner_children[parent].add(child)
        self.steiner_nodes.add(parent)
        self.steiner_nodes.add(child)

        if parent not in self.steiner_junctions and len(self.steiner_children[parent]) > 1:
            pp = parent
            while pp not in self.steiner_junctions:
                if pp not in self.steiner_parents:
                    break
                pp = self.steiner_parents[pp]

            cc = parent
            while cc not in self.steiner_junctions:
                if cc not in self.steiner_children:
                    break
                next_child = None
                for candidate in self.steiner_children[cc]:
                    if candidate != child:
                        next_child = candidate
                        break
                if next_child is None:
                    break
                cc = next_child

            self.steiner_paths[pp].add(parent)
            self.steiner_paths[parent].add(cc)
            self.steiner_paths[pp].discard(cc)
            if not self.steiner_paths[pp]:
                self.steiner_paths.pop(pp, None)
            self.steiner_junctions.add(parent)

            to_check.add((pp, parent))
            to_check.add((parent, cc))
            to_check.discard((pp, cc))

    def _find_best_path(self, find_input):
        write_index, parent_node, starting_node = find_input
        actual_node = starting_node
        actual_distance = 0
        while actual_node in self.steiner_parents:
            actual_distance += 1
            if self.steiner_parents[actual_node] == parent_node:
                break
            actual_node = self.steiner_parents[actual_node]

        valid = set()
        to_be_explored = deque([parent_node])
        while to_be_explored:
            node = to_be_explored.popleft()
            for child in self.steiner_children.get(node, ()):
                if child == actual_node:
                    continue
                valid.add(child)
                to_be_explored.append(child)

        parent = {}
        distance = {starting_node: 0}
        explored = set()
        to_be_explored.append(starting_node)

        while to_be_explored:
            actual_node = to_be_explored.popleft()
            start = self.edges_index[actual_node]
            end = self.edges_index[actual_node + 1]
            child_to_check = set(self.edges[start:end])

            for child in child_to_check:
                if child in parent:
                    continue
                if child in explored:
                    continue

                parent[child] = actual_node
                distance[child] = distance[actual_node] + 1
                if distance[child] >= actual_distance:
                    continue

                if child not in valid:
                    to_be_explored.append(child)
                    continue

                remaining_distance = actual_distance - distance[child] + self.delta_excess
                temp = child
                while temp != parent_node:
                    temp = self.steiner_parents[temp]
                    remaining_distance -= 1
                    if remaining_distance < 0:
                        break
                if remaining_distance < 0:
                    continue

                best_path = []
                temp = child
                while temp in parent:
                    best_path.append(temp)
                    temp = parent[temp]
                best_path.append(starting_node)
                return write_index, best_path, actual_distance - distance[child]

            explored.add(actual_node)

        return write_index, [], 0

    def _subdivide_power_lines(self):
        self.power_lines_consumption = {}
        self.power_lines_path = defaultdict(set)
        self.lines_patch = defaultdict(set)

        order = []
        to_be_explored = deque([self.start_node])
        order.append((self.start_node, -1))

        while to_be_explored:
            actual_node = to_be_explored.popleft()
            if actual_node in self.steiner_paths:
                for child in self.steiner_paths[actual_node]:
                    order.append((child, actual_node))
                    to_be_explored.append(child)

        power_line_id = 0

        for actual_node, parent_node in reversed(order):
            if actual_node not in self.steiner_paths and actual_node != -1:
                self.power_lines_consumption[power_line_id] = self.power_node_consumptions.get(actual_node, 0)
                self.power_lines_path[power_line_id].add(actual_node)
                self.lines_patch[actual_node].add(power_line_id)
                power_line_id += 1
            else:
                to_be_merged = [
                    (line, self.power_lines_consumption[line])
                    for line in self.lines_patch.get(actual_node, ())
                ]
                to_be_merged.sort(key=lambda item: item[1], reverse=True)

                index_a = 0
                while index_a < len(to_be_merged):
                    index_b = index_a + 1
                    while index_b < len(to_be_merged):
                        id_a = to_be_merged[index_a][0]
                        id_b = to_be_merged[index_b][0]
                        merged_consumption = (
                            self.power_lines_consumption[id_a]
                            + self.power_lines_consumption[id_b]
                        )
                        if merged_consumption > self.max_power_for_line:
                            index_b += 1
                            continue

                        self.power_lines_consumption[id_a] = merged_consumption
                        for node in list(self.power_lines_path[id_b]):
                            self.power_lines_path[id_a].add(node)
                            self.power_lines_path[id_b].discard(node)
                            self.lines_patch[node].discard(id_b)
                            self.lines_patch[node].add(id_a)

                        self.power_lines_path.pop(id_b, None)
                        self.power_lines_consumption.pop(id_b, None)
                        self.lines_patch[actual_node].discard(id_b)
                        to_be_merged.pop(index_b)
                    index_a += 1

            if parent_node == -1:
                continue

            for line in list(self.lines_patch.get(actual_node, ())):
                self.lines_patch[parent_node].add(line)

    def _populate_power_lines_per_node(self):
        self.power_lines_per_node = {
            node: set()
            for node in self.steiner_parents.keys()
        }
        self.power_lines_per_node[self.start_node] = set(self.power_lines_consumption.keys())

        for line in list(self.power_lines_consumption.keys()):
            to_explore = set(self.power_lines_path.get(line, ()))
            for node in to_explore:
                actual = node
                while actual in self.steiner_parents:
                    self.power_lines_per_node.setdefault(actual, set())
                    if line in self.power_lines_per_node[actual]:
                        break
                    self.power_lines_per_node[actual].add(line)
                    actual = self.steiner_parents[actual]
