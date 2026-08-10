"""
State connector registry. Each entry is a state_id -> connector class.

To add a state: write a new module implementing states.base.StateConnector,
then register it here. Nothing else in the pipeline needs to change.
"""
from states.haryana import HaryanaConnector
from states.karnataka import KarnatakaConnector

CONNECTORS = {
    "karnataka": KarnatakaConnector,
    "haryana": HaryanaConnector,
}


def get_connector(state_id):
    if state_id not in CONNECTORS:
        raise ValueError(
            f"No connector registered for '{state_id}'. "
            f"Available: {', '.join(CONNECTORS)}"
        )
    return CONNECTORS[state_id]()
