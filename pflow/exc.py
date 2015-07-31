class FlowError(Exception):
    """
    Error in the flow design.
    """
    pass


class GraphRuntimeError(FlowError):
    pass


class ComponentError(Exception):
    """
    Component-level error.
    """
    pass


class ComponentStateError(ComponentError):
    """
    Component-level state/transition error.
    """
    pass


class PortError(ComponentError):
    """
    Port error.
    """
    pass


class PortClosedError(PortError):
    """
    Port is closed.
    """
    pass
