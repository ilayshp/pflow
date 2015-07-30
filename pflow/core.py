from abc import ABCMeta, abstractmethod
import logging
import json

try:
    from queue import Queue  # 3.x
except ImportError:
    from Queue import Queue  # 2.x

from enum import Enum

from . import utils, exc
from . import parsefbp
from .port import Packet, PortRegistry, InputPort, OutputPort, ArrayInputPort, ArrayOutputPort
from .runtimes.base import RuntimeTarget

log = logging.getLogger(__name__)


class ComponentState(Enum):

    # Initial component state before it receives first packet.
    NOT_STARTED = 'NOT_STARTED'

    # Component has received data and is actively running.
    ACTIVE = 'ACTIVE'

    # Component processed last data item and is SUSPENDED.
    # until the next packet arrives.
    SUSPENDED = 'SUSPENDED'

    # Component has either TERMINATED itself or has processed
    # the last packet. This component is automatically considered
    # TERMINATED it: 1) has no more data to process, and 2) all its
    # upstream components are TERMINATED.
    TERMINATED = 'TERMINATED'

    # Component has been TERMINATED with an ERROR status. This can
    # happen when an unexpected exception is raised.
    ERROR = 'ERROR'


class Component(RuntimeTarget):
    """
    Component instances are "process" nodes in a flow-based digraph.

    Each Component has zero or more input and output ports, which are
    connected to other Components through Ports.
    """
    __metaclass__ = ABCMeta

    # Valid state transitions for components
    _valid_transitions = frozenset([
        (ComponentState.NOT_STARTED, ComponentState.ACTIVE),
        (ComponentState.NOT_STARTED, ComponentState.TERMINATED),

        (ComponentState.ACTIVE, ComponentState.SUSPENDED),
        (ComponentState.ACTIVE, ComponentState.TERMINATED),
        (ComponentState.ACTIVE, ComponentState.ERROR),

        (ComponentState.SUSPENDED, ComponentState.ACTIVE),
        (ComponentState.SUSPENDED, ComponentState.TERMINATED),
        (ComponentState.SUSPENDED, ComponentState.ERROR)
    ])

    def __init__(self, name):
        if not isinstance(name, basestring):
            raise ValueError('name must be a string')

        self.name = name
        self.inputs = PortRegistry(self, InputPort, ArrayInputPort)
        self.outputs = PortRegistry(self, OutputPort, ArrayOutputPort)
        self._state = ComponentState.NOT_STARTED
        self.owned_packet_count = 0
        self.log = logging.getLogger('%s(%s)' % (self.__class__.__name__,
                                                 self.name))
        self.initialize()

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, new_state):
        if not isinstance(new_state, ComponentState):
            raise ValueError('new_state must be a value from the ComponentState enum')

        old_state = self._state

        if old_state == new_state:
            return

        # Ensure state transition is a valid one.
        if (old_state, new_state) not in self._valid_transitions:
            raise exc.ComponentStateError('Invalid state transition for %s: %s -> %s' %
                                          (self, old_state.value, new_state.value))

        self._state = new_state

        self.log.debug('Transitioned from %s -> %s' %
                       (old_state.value, new_state.value))

        # TODO: Fire a transition event

    # @abstractmethod
    def initialize(self):
        """
        Initialization code necessary to define this component.
        """
        pass

    @abstractmethod
    def run(self):
        """
        This method is called any time the port is open and a new Packet arrives.
        """
        pass

    def destroy(self):
        """
        Implementations can override this to call any cleanup code when the component
        has transitioned to a TERMINATED state and is about to be destroyed.
        """
        self.log.debug('Destroyed')

    def create_packet(self, value=None):
        """
        Create a new packet and set self as owner.

        :param value: initial value for the packet.
        :return: a new Packet.
        """
        packet = Packet(value)
        packet.owner = self

        self.owned_packet_count += 1
        return packet

    def drop(self, packet):
        """
        Drop a Packet.
        """
        if not isinstance(packet, Packet):
            raise ValueError('packet must be a Packet')

        if packet.owner == self:
            self.owned_packet_count -= 1

        del packet

    @property
    def is_terminated(self):
        """
        Has this component been terminated?
        """
        return self.state in (ComponentState.TERMINATED,
                              ComponentState.ERROR)

    def terminate(self, ex=None):
        """
        Terminate execution for this component.
        This will not terminate upstream components!
        """
        if self.is_terminated:
            return

        if ex is not None:
            if not isinstance(ex, Exception):
                raise ValueError('ex must be an Exception')

            self.log.error('Abnormally terminating because of %s...' %
                           ex.__class__.__name__)
            self.state = ComponentState.ERROR
        else:
            self.state = ComponentState.TERMINATED

        # Close all input ports
        for in_port in self.inputs:
            if in_port.is_open():
                in_port.close()

        self.runtime.terminate_thread(self)

    def suspend(self, seconds=None):
        """
        Yield execution to scheduler.
        """
        if self.is_terminated:
            self.runtime.suspend_thread(seconds)
        else:
            self.state = ComponentState.SUSPENDED
            self.runtime.suspend_thread(seconds)
            if not self.is_terminated:
                self.state = ComponentState.ACTIVE

    def __str__(self):
        return ('Component(%s, inputs=%s, outputs=%s)' %
                (self.name, self.inputs, self.outputs))


class InitialPacketGenerator(Component):
    """
    An initial packet (IIP) generator that is connected to an input port
    of a component.

    This should have no input ports, a single output port, and is used to
    make the logic easier.
    """
    def __init__(self, value):
        self.value = value
        super(InitialPacketGenerator, self).__init__('IIP_GEN_%s' %
                                                     utils.random_id())

    def initialize(self):
        self.outputs.add(OutputPort('OUT'))

    def run(self):
        self.outputs['OUT'].send(self.value)


class Graph(Component):
    """
    Execution graph.

    This can also be used as a subgraph for composite components.
    """
    __metaclass__ = ABCMeta

    def __init__(self, *args, **kwargs):
        self.components = set()  # Nodes
        super(Graph, self).__init__(*args, **kwargs)

    def get_upstream(self, component):
        """
        Immediate upstream components.
        """
        upstream = set()

        for port in component.inputs:
            if port.is_connected():
                upstream.add(port.source_port.component)

        return upstream

    def get_downstream(self, component):
        """
        Immediate downstream components.
        """
        downstream = set()

        for port in component.outputs:
            if port.is_connected():
                downstream.add(port.target_port.component)

        return downstream

    def is_upstream_terminated(self, component):
        """
        Are all of a component's upstream components terminated?

        :param component: the component to check.
        :return: whether or not the upstream components have been terminated.
        """
        dead_parents = all([c.is_terminated for c in self.get_upstream(component)])
        return dead_parents

    def add_component(self, component):
        if not isinstance(component, Component):
            raise ValueError('component must be a Component instance')

        # Already added?
        if component in self.components:
            return component

        # Unique name?
        used_names = utils.pluck(self.components, 'name')
        if component.name in used_names:
            raise ValueError('Component name "%s" has already been used in this graph' % component.name)

        self.components.add(component)
        return component

    def set_initial_packet(self, port, value):
        iip = InitialPacketGenerator(value)
        self.connect(iip.outputs['OUT'], port)
        return iip

    def connect(self, source_output_port, target_input_port):
        """
        Connect components by their ports.

        :param source_output_port: the output port on the source component.
        :param target_input_port: the input port on the target component.
        """
        if not isinstance(source_output_port, (OutputPort, ArrayOutputPort)):
            raise ValueError('source_output_port must be an output port')

        if not isinstance(target_input_port, (InputPort, ArrayInputPort)):
            raise ValueError('target_input_port must be an input port')

        if target_input_port.source_port is not None:
            raise exc.PortError('target_input_port is already connected to '
                                'another source')

        log.debug('%s connected to %s' % (source_output_port, target_input_port))

        self.add_component(source_output_port.component)
        self.add_component(target_input_port.component)

        # FIXME: make these private?
        source_output_port.target_port = target_input_port
        target_input_port.source_port = source_output_port

    @property
    def self_starters(self):
        """
        Returns a set of all self-starter components.
        """
        # Has only disconnected optional inputs
        def is_self_starter_input(input_port):
            return (input_port.optional and
                    input_port.source_port is None)

        self_starters = set()
        for node in self.components:
            # Self-starter nodes should have either no inputs or only
            # have disconnected optional inputs.
            if len(node.inputs) == 0:
                # No inputs
                self_starters.add(node)
            elif all(map(is_self_starter_input, node.inputs)):
                self_starters.add(node)

        return self_starters

    def run(self):
        raise RuntimeError('Instead of calling Graph.run(), please use a GraphRuntime to run this Graph')

    @property
    def is_terminated(self):
        """
        Has this graph been terminated?
        """
        return all([component.is_terminated for component in self.components])

    # TODO: move all serializers to their own module / abstract class
    def load_fbp_string(self, fbp_script):
        if not isinstance(fbp_script, basestring):
            raise ValueError('fbp_script must be a string')

        # TODO: parse fbp string and build graph
        #raise NotImplementedError
        pass

    def load_fbp_file(self, file_path):
        if not isinstance(file_path, basestring):
            raise ValueError('file_path must be a string')

        with open(file_path, 'r') as f:
            self.load_fbp_string(f.read())

    def load_json_dict(self, json_dict):
        # TODO: parse json dict and build graph
        raise NotImplementedError

    def load_json_file(self, file_path):
        if not isinstance(file_path, basestring):
            raise ValueError('file_path must be a string')

        with open(file_path) as f:
            json_dict = json.loads(f.read())
            self.load_json_dict(json_dict)

    def write_graphml(self, file_path):
        """
        Writes this Graph as a *.graphml file.

        This is useful for debugging network configurations that are hard
        to visualize purely with code.

        :param file_path: the file to write to (should have a .graphml extension)
        """
        import networkx as nx

        if not isinstance(file_path, basestring):
            raise ValueError('file_path must be a string')

        graph = nx.DiGraph()

        def build_nodes(components):
            visited_nodes = set()

            for component in components:
                if component not in visited_nodes:
                    visited_nodes.add(component)
                    graph.add_node(component.name)

                node_attribs = {
                    'label': '%s\n(%s)' % (component.name,
                                           component.__class__.__name__),
                    'description': (component.__class__.__doc__ or '')
                }

                graph.node[component.name].update(node_attribs)

        def build_edges(components, visited_nodes=None):
            if visited_nodes is None:
                visited_nodes = set()

            if len(components) > 0:
                next_components = set()

                for component in components:
                    if component in visited_nodes:
                        continue

                    for output in component.outputs:
                        next_components.add(output.target_port.component)

                        edge_attribs = {
                            'label': '%s -> %s' % (output.name,
                                                   output.target_port.name),
                            'description': (output.description or '')
                        }

                        graph.add_edge(component.name,
                                       output.target_port.component.name,
                                       edge_attribs)

                    visited_nodes.add(component)

                build_edges(next_components, visited_nodes)

        self.log.debug('Building nx graph...')
        build_nodes(self.components)
        build_edges(self.components)

        self.log.debug('Writing graph to "%s"...' % file_path)
        nx.write_graphml(graph, file_path)
