# Copyright 2019 Robert Bosch GmbH
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Base processor module."""

from collections import defaultdict
import sys
from typing import Callable
from typing import Dict
from typing import List
from typing import Set
from typing import Type

from tracetools_read import DictEvent
from tracetools_read import get_event_name
from tracetools_read import get_field


class EventMetadata():
    """Container for event metadata."""

    def __init__(
        self,
        event_name: str,
        timestamp: int,
        cpu_id: int,
        procname: str = None,
        pid: int = None,
        tid: int = None,
    ) -> None:
        """
        Create an EventMetadata.

        Parameters with a default value of `None` are not mandatory,
        since they are not always present.
        """
        self._event_name = event_name
        self._timestamp = timestamp
        self._cpu_id = cpu_id
        self._procname = procname
        self._pid = pid
        self._tid = tid

    @property
    def event_name(self):
        return self._event_name

    @property
    def timestamp(self):
        return self._timestamp

    @property
    def cpu_id(self):
        return self._cpu_id

    @property
    def procname(self):
        return self._procname

    @property
    def pid(self):
        return self._pid

    @property
    def tid(self):
        return self._tid


HandlerMap = Dict[str, Callable[[DictEvent, EventMetadata], None]]
HandlerMultimap = Dict[str, List[Callable[[DictEvent, EventMetadata], None]]]


class Dependant():
    """
    Object which depends on other types.

    A dependant depends on other types which might have dependencies themselves.
    Dependencies are type-related only.
    """

    @staticmethod
    def dependencies() -> List[Type['Dependant']]:
        """
        Get the dependencies that should also exist along with this current one.

        Subclasses should override this method if they want to declare dependencies.
        Default: no dependencies.
        """
        return []


class EventHandler(Dependant):
    """
    Base event handling class.

    Provides handling functions for some events, depending on the name.
    Passes that on to a data model. To be subclassed.
    """

    def __init__(
        self,
        *,
        handler_map: HandlerMap,
        **kwargs,
    ) -> None:
        """
        Create an EventHandler.

        TODO make subclasses pass on their *DataModel to this class

        :param handler_map: the mapping from event name to handling method
        """
        assert handler_map is not None and len(handler_map) > 0, \
            f'empty map: {self.__class__.__name__}'
        assert all(required_name in handler_map.keys() for required_name in self.required_events())
        self._handler_map = handler_map
        self.processor = None

    @property
    def handler_map(self) -> HandlerMap:
        """Get the handler functions map."""
        return self._handler_map

    @property
    def data(self) -> None:
        """Get the data model."""
        return None

    @staticmethod
    def required_events() -> Set[str]:
        """
        Get the set of events required by this EventHandler.

        Without these events, the EventHandler would be invalid/useless. Inheriting classes can
        decide not to declare that they require specific events.
        """
        return {}

    def register_processor(self, processor: 'Processor') -> None:
        """Register processor with this `EventHandler` so that it can query other handlers."""
        self.processor = processor

    @staticmethod
    def int_to_hex_str(addr: int) -> str:
        """Format an `int` into an hex `str`."""
        return f'0x{addr:X}'

    @classmethod
    def process(cls, events: List[DictEvent], **kwargs) -> 'EventHandler':
        """
        Create a `Processor` and process an instance of the class.

        :param events: the list of events
        :return: the processor object after processing
        """
        assert cls != EventHandler, 'only call process() from inheriting classes'
        handler_object = cls(**kwargs)  # pylint: disable=all
        processor = Processor(handler_object, **kwargs)
        processor.process(events)
        return handler_object


class DependencySolver():
    """
    Solve `Dependant` dependencies.

    Post-order depth-first search (ish). Does not check for circular dependencies or other errors.
    """

    def __init__(
        self,
        *initial_dependants: Dependant,
        **kwargs,
    ) -> None:
        """
        Create a DependencySolver.

        :param initial_dependants: the initial dependant instances, in order
        :param kwargs: the parameters to pass on to new instances
        """
        self._initial_deps = list(initial_dependants)
        self._kwargs = kwargs

    def solve(self) -> List[Dependant]:
        """
        Solve.

        :return: the solved list, including at least the initial dependants, in order
        """
        visited: Set[Type[Dependant]] = set()
        solution: List[Dependant] = []
        initial_map = {type(dep_instance): dep_instance for dep_instance in self._initial_deps}
        for dep_instance in self._initial_deps:
            self.__solve_instance(
                dep_instance,
                visited,
                initial_map,
                solution,
            )
        return solution

    def __solve_instance(
        self,
        dep_instance: Dependant,
        visited: Set[Type[Dependant]],
        initial_map: Dict[Type[Dependant], Dependant],
        solution: List[Dependant],
    ) -> None:
        if type(dep_instance) not in visited:
            for dependency_type in type(dep_instance).dependencies():
                self.__solve_type(
                    dependency_type,
                    visited,
                    initial_map,
                    solution,
                )
            solution.append(dep_instance)
            visited.add(type(dep_instance))

    def __solve_type(
        self,
        dep_type: Type[Dependant],
        visited: Set[Type[Dependant]],
        initial_map: Dict[Type[Dependant], Dependant],
        solution: List[Dependant],
    ) -> None:
        if dep_type not in visited:
            for dependency_type in dep_type.dependencies():
                self.__solve_type(
                    dependency_type,
                    visited,
                    initial_map,
                    solution,
                )
            # If an instance of this type was given initially, use it instead
            new_instance = None
            if dep_type in initial_map:
                new_instance = initial_map.get(dep_type)
            else:
                new_instance = dep_type(**self._kwargs)
            solution.append(new_instance)
            visited.add(dep_type)


class RequiredEventNotFoundError(RuntimeError):
    pass


class Processor():
    """Processor class, which dispatches events to event handlers."""

    def __init__(
        self,
        *handlers: EventHandler,
        **kwargs,
    ) -> None:
        """
        Create a Processor.

        :param handlers: the `EventHandler`s to use for processing
        :param kwargs: the parameters to pass on to new handlers
        """
        self._initial_handlers = list(handlers)
        if len(self._initial_handlers) == 0:
            raise RuntimeError('Must provide at least one handler!')
        self._expanded_handlers = self._expand_dependencies(*handlers, **kwargs)
        self._handler_multimap = self._get_handler_maps(self._expanded_handlers)
        self._register_with_handlers(self._expanded_handlers)
        self._progress_display = ProcessingProgressDisplay(
            [type(handler).__name__ for handler in self._expanded_handlers],
        )
        self._processing_done = False

    @staticmethod
    def _expand_dependencies(
        *handlers: EventHandler,
        **kwargs,
    ) -> List[EventHandler]:
        """
        Check handlers and add dependencies if not included.

        :param handlers: the list of primary `EventHandler`s
        :param kwargs: the parameters to pass on to new instances
        """
        return DependencySolver(*handlers, **kwargs).solve()

    @staticmethod
    def _get_handler_maps(
        handlers: List[EventHandler],
    ) -> HandlerMultimap:
        """
        Collect and merge `HandlerMap`s from all events handlers into a `HandlerMultimap`.

        :param handlers: the list of handlers
        :return: the merged multimap
        """
        handler_multimap = defaultdict(list)
        for handler in handlers:
            for event_name, handler in handler.handler_map.items():
                handler_multimap[event_name].append(handler)
        return handler_multimap

    def _register_with_handlers(
        self,
        handlers: List[EventHandler],
    ) -> None:
        """
        Register this processor with its `EventHandler`s.

        :param handlers: the list of handlers
        """
        for handler in handlers:
            handler.register_processor(self)

    @staticmethod
    def get_event_names(
        events: List[DictEvent],
    ) -> Set[str]:
        """Get set of names from a list of events."""
        return {get_event_name(event) for event in events}

    def _check_required_events(
        self,
        events: List[DictEvent],
    ) -> None:
        event_names = self.get_event_names(events)
        # Check names separately so that we can know which event from which handler is missing
        for handler in self._expanded_handlers:
            for name in handler.required_events():
                if name not in event_names:
                    raise RequiredEventNotFoundError(
                        f'missing event {name} for {handler.__class__.__name__}'
                    )

    def process(
        self,
        events: List[DictEvent],
        erase_progress: bool = False,
        no_required_events_check: bool = False,
    ) -> None:
        """
        Process all events.

        :param events: the events to process
        :param erase_progress: whether to erase the progress message
        :param no_required_events_check: whether to skip the check for required events
        """
        if not no_required_events_check:
            self._check_required_events(events)

        if not self._processing_done:
            self._progress_display.set_work_total(len(events))
            for event in events:
                self._process_event(event)
                self._progress_display.did_work()
            self._processing_done = True
            self._progress_display.done(erase=erase_progress)

    def _process_event(self, event: DictEvent) -> None:
        """Process a single event."""
        event_name = get_event_name(event)
        handler_functions = self._handler_multimap.get(event_name, None)
        if handler_functions is not None:
            for handler_function in handler_functions:
                timestamp = get_field(event, '_timestamp')
                cpu_id = get_field(event, 'cpu_id')
                # TODO perhaps validate fields depending on the type of event,
                # i.e. all UST events should have procname, (v)pid and (v)tid
                # context info, since analyses might not work otherwise
                procname = get_field(event, 'procname', raise_if_not_found=False)
                pid = get_field(
                    event,
                    'vpid',
                    default=get_field(
                        event,
                        'pid',
                        raise_if_not_found=False),
                    raise_if_not_found=False)
                tid = get_field(
                    event,
                    'vtid',
                    default=get_field(
                        event,
                        'tid',
                        raise_if_not_found=False),
                    raise_if_not_found=False)
                metadata = EventMetadata(event_name, timestamp, cpu_id, procname, pid, tid)
                handler_function(event, metadata)

    def print_data(self) -> None:
        """Print processed data."""
        if self._processing_done:
            for handler in self._initial_handlers:
                handler.data.print_data()


class AutoProcessor():
    """
    Automatic processor, which takes a list of events and enables all relevant handlers.

    It checks each existing EventHandler, and, if its required events are in the events list, it
    uses that handler.
    """

    def __init__(
        self,
        events: List[DictEvent],
        **kwargs,
    ) -> None:
        """
        Create an AutoProcessor.

        :param events: the list of events to process
        :param kwargs: the kwargs to provide when instanciating EventHandler subclasses
        """
        handlers = self.get_applicable_event_handlers(events)
        processor = Processor(
            *handlers,
            **kwargs,
        )
        processor.process(events)

    @staticmethod
    def get_applicable_event_handlers(
        events: List[DictEvent],
    ) -> List[EventHandler]:
        """
        Get applicable EventHandler instances for a list of events.

        :param events: the list of events
        :return the concrete EventHandler instances which are applicable
        """
        event_names = Processor.get_event_names(events)
        # Force import of all processor submodules (i.e. files) so that we can find all
        # EventHandler subclasses
        AutoProcessor._import_event_handler_submodules()
        all_handler_classes = AutoProcessor._get_subclasses(EventHandler)
        applicable_handler_classes = AutoProcessor._get_applicable_event_handler_classes(
            event_names,
            all_handler_classes,
        )
        return AutoProcessor._get_event_handler_instances(applicable_handler_classes)

    @staticmethod
    def _get_applicable_event_handler_classes(
        event_names: List[str],
        handler_classes: List[Type[EventHandler]],
    ) -> List[Type[EventHandler]]:
        """
        Get applicable EventHandler subclasses for a list of event names.

        :param event_names: the list of event names
        :return: a list of EventHandler subclasses for which requirements are met
        """
        return [
            handler for handler in handler_classes
            if set(handler.required_events()).issubset(event_names)
        ]

    @staticmethod
    def _get_event_handler_instances(
        handler_classes: List[Type[EventHandler]],
        **kwargs,
    ) -> List[EventHandler]:
        """
        Create instances from a list of EventHandlers (sub)classes.

        :param handler_classes: the list of EventHandler subclasses
        :param kwargs: the kwargs to provide when instanciating EventHandler subclasses
        :return: the list of concrete instances
        """
        # Doing this manually to catch exceptions, e.g. when a given EventHandler subclass is
        # abstract and thus should not be instanciated
        handlers = []
        for handler_class in handler_classes:
            try:
                instance = handler_class(**kwargs)
                handlers.append(instance)
            except:
                pass
        return handlers

    @staticmethod
    def _get_subclasses(
        cls: Type,
    ) -> Set[Type]:
        """Get all subclasses of a class recursively."""
        return set(cls.__subclasses__()) | {
            subsubcls for subcls in cls.__subclasses__() for subsubcls in AutoProcessor._get_subclasses(subcls)
        }

    @staticmethod
    def _import_event_handler_submodules(recursive=True):
        """Force import of EventHandler submodules."""
        import importlib
        import pkgutil
        package = importlib.import_module(__name__)
        results = {}
        for loader, name, is_pkg in pkgutil.walk_packages(package.__path__):
            full_name = package.__name__ + '.' + name
            results[full_name] = importlib.import_module(full_name)
            if recursive and is_pkg:
                results.update(_import_event_handler_submodules(full_name))
        return results


class ProcessingProgressDisplay():
    """Display processing progress periodically on stdout."""

    def __init__(
        self,
        processing_elements: List[str],
    ) -> None:
        """
        Create a ProcessingProgressDisplay.

        :param processing_elements: the list of elements doing processing
        """
        self.__info_string = '[' + ', '.join(processing_elements) + ']'
        self.__total_work = None
        self.__progress_count = None
        self.__rolling_count = None
        self.__work_display_period = None

    def set_work_total(
        self,
        total: int,
    ) -> None:
        """
        Set the total units of work.

        :param total: the total number of units of work to do
        """
        self.__total_work = total
        self.__progress_count = 0
        self.__rolling_count = 0
        self.__work_display_period = total // 100
        self._update()

    def did_work(
        self,
        increment: int = 1,
    ) -> None:
        """
        Increment the amount of work done.

        :param increment: the number of units of work to add to the total
        """
        # For now, let it fail if set_work_total() hasn't been called
        self.__progress_count += increment
        self.__rolling_count += increment
        if self.__rolling_count >= self.__work_display_period:
            self.__rolling_count -= self.__work_display_period
            self._update()

    def _get_progress_message(
        self,
        percentage: float,
    ) -> str:
        return f' [{percentage:2.0f}%] {self.__info_string}'

    def _update(
        self,
    ) -> None:
        percentage = 100.0 * (float(self.__progress_count) / float(self.__total_work))
        sys.stdout.write(self._get_progress_message(percentage) + '\r')

    def done(
        self,
        erase: bool = False,
    ) -> None:
        """
        Set progress to done.

        :param erase: whether to erase the progress message
        """
        if erase:
            # Write spaces over progress message to "erase" it
            sys.stdout.write(len(self._get_progress_message(100.0)) * ' ' + '\r')
        sys.stdout.write('\n')
