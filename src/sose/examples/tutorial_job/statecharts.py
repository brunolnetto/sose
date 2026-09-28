from statemachine import State, StateChart

from sose.api import probabilistic_transitions


@probabilistic_transitions({}, excluded_events={"start", "finish"})
class TutorialJobChart(StateChart):
    queued = State(initial=True)
    running = State()
    completed = State(final=True)

    start = queued.to(running)
    finish = running.to(completed)
