from rich import print

import release

from .state import save

STEP_COUNTER = 0


class Command:
    track_steps_on_attr = "release"

    def __init__(self, release: "release.state.Release"):
        self.release = release

    @property
    def steps(self):
        return getattr(self, self.track_steps_on_attr).steps

    def __call__(self):
        print(f"[blue bold]{self.__class__.__doc__.format(self=self)}")
        print()

        steps = [getattr(self, x) for x in dir(self)]
        steps = [x for x in steps if hasattr(x, "_cmd_step_order")]
        steps.sort(key=lambda s: s._cmd_step_order)

        for step in steps:
            step_id = step.__qualname__

            header = step.__doc__.format(self=self).strip()
            print(f"[bold]{header}")

            if step._cmd_skip_seen and step_id in self.steps:
                print("[italic cyan]Skipping - already done.[/italic cyan]")
                print()
                continue
            print()
            step()
            print()
            self.steps.add(step_id)
            save(self.release)


def step(*args, skip_seen=True):
    def _step(method):
        global STEP_COUNTER
        STEP_COUNTER += 1
        method._cmd_step_order = STEP_COUNTER
        method._cmd_skip_seen = skip_seen
        return method

    if len(args) == 1 and callable(args[0]):
        # No arguments, this is the decorator
        # Set default values for the arguments
        return _step(args[0])
    else:
        return _step
