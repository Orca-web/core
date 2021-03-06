"""Template helper methods for rendering strings with Home Assistant data."""
from ast import literal_eval
import asyncio
import base64
import collections.abc
from datetime import datetime, timedelta
from functools import wraps
import json
import logging
import math
from operator import attrgetter
import random
import re
from typing import Any, Generator, Iterable, List, Optional, Union
from urllib.parse import urlencode as urllib_urlencode
import weakref

import jinja2
from jinja2 import contextfilter, contextfunction
from jinja2.sandbox import ImmutableSandboxedEnvironment
from jinja2.utils import Namespace  # type: ignore
import voluptuous as vol

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_UNIT_OF_MEASUREMENT,
    LENGTH_METERS,
    MATCH_ALL,
    STATE_UNKNOWN,
)
from homeassistant.core import State, callback, split_entity_id, valid_entity_id
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import config_validation as cv, location as loc_helper
from homeassistant.helpers.frame import report
from homeassistant.helpers.typing import HomeAssistantType, TemplateVarsType
from homeassistant.loader import bind_hass
from homeassistant.util import convert, dt as dt_util, location as loc_util
from homeassistant.util.async_ import run_callback_threadsafe
from homeassistant.util.thread import ThreadWithException

# mypy: allow-untyped-calls, allow-untyped-defs
# mypy: no-check-untyped-defs, no-warn-return-any

_LOGGER = logging.getLogger(__name__)
_SENTINEL = object()
DATE_STR_FORMAT = "%Y-%m-%d %H:%M:%S"

_RENDER_INFO = "template.render_info"
_ENVIRONMENT = "template.environment"

_RE_NONE_ENTITIES = re.compile(r"distance\(|closest\(", re.I | re.M)
_RE_GET_ENTITIES = re.compile(
    r"(?:(?:(?:states\.|(?P<func>is_state|is_state_attr|state_attr|states|expand)\((?:[\ \'\"]?))(?P<entity_id>[\w]+\.[\w]+)|states\.(?P<domain_outer>[a-z]+)|states\[(?:[\'\"]?)(?P<domain_inner>[\w]+))|(?P<variable>[\w]+))",
    re.I | re.M,
)

_RE_JINJA_DELIMITERS = re.compile(r"\{%|\{\{|\{#")

_RESERVED_NAMES = {"contextfunction", "evalcontextfunction", "environmentfunction"}

_GROUP_DOMAIN_PREFIX = "group."

_COLLECTABLE_STATE_ATTRIBUTES = {
    "state",
    "attributes",
    "last_changed",
    "last_updated",
    "context",
    "domain",
    "object_id",
    "name",
}

DEFAULT_RATE_LIMIT = timedelta(minutes=1)


@bind_hass
def attach(hass: HomeAssistantType, obj: Any) -> None:
    """Recursively attach hass to all template instances in list and dict."""
    if isinstance(obj, list):
        for child in obj:
            attach(hass, child)
    elif isinstance(obj, collections.abc.Mapping):
        for child_key, child_value in obj.items():
            attach(hass, child_key)
            attach(hass, child_value)
    elif isinstance(obj, Template):
        obj.hass = hass


def render_complex(value: Any, variables: TemplateVarsType = None) -> Any:
    """Recursive template creator helper function."""
    if isinstance(value, list):
        return [render_complex(item, variables) for item in value]
    if isinstance(value, collections.abc.Mapping):
        return {
            render_complex(key, variables): render_complex(item, variables)
            for key, item in value.items()
        }
    if isinstance(value, Template):
        return value.async_render(variables)

    return value


def is_complex(value: Any) -> bool:
    """Test if data structure is a complex template."""
    if isinstance(value, Template):
        return True
    if isinstance(value, list):
        return any(is_complex(val) for val in value)
    if isinstance(value, collections.abc.Mapping):
        return any(is_complex(val) for val in value.keys()) or any(
            is_complex(val) for val in value.values()
        )
    return False


def is_template_string(maybe_template: str) -> bool:
    """Check if the input is a Jinja2 template."""
    return _RE_JINJA_DELIMITERS.search(maybe_template) is not None


def extract_entities(
    hass: HomeAssistantType,
    template: Optional[str],
    variables: TemplateVarsType = None,
) -> Union[str, List[str]]:
    """Extract all entities for state_changed listener from template string."""

    report(
        "called template.extract_entities. Please use event.async_track_template_result instead as it can accurately handle watching entities"
    )

    if template is None or not is_template_string(template):
        return []

    if _RE_NONE_ENTITIES.search(template):
        return MATCH_ALL

    extraction_final = []

    for result in _RE_GET_ENTITIES.finditer(template):
        if (
            result.group("entity_id") == "trigger.entity_id"
            and variables
            and "trigger" in variables
            and "entity_id" in variables["trigger"]
        ):
            extraction_final.append(variables["trigger"]["entity_id"])
        elif result.group("entity_id"):
            if result.group("func") == "expand":
                for entity in expand(hass, result.group("entity_id")):
                    extraction_final.append(entity.entity_id)

            extraction_final.append(result.group("entity_id"))
        elif result.group("domain_inner") or result.group("domain_outer"):
            extraction_final.extend(
                hass.states.async_entity_ids(
                    result.group("domain_inner") or result.group("domain_outer")
                )
            )

        if (
            variables
            and result.group("variable") in variables
            and isinstance(variables[result.group("variable")], str)
            and valid_entity_id(variables[result.group("variable")])
        ):
            extraction_final.append(variables[result.group("variable")])

    if extraction_final:
        return list(set(extraction_final))
    return MATCH_ALL


def _true(arg: Any) -> bool:
    return True


def _false(arg: Any) -> bool:
    return False


class RenderInfo:
    """Holds information about a template render."""

    def __init__(self, template):
        """Initialise."""
        self.template = template
        # Will be set sensibly once frozen.
        self.filter_lifecycle = _true
        self.filter = _true
        self._result = None
        self.is_static = False
        self.exception = None
        self.all_states = False
        self.all_states_lifecycle = False
        self.domains = set()
        self.domains_lifecycle = set()
        self.entities = set()
        self.rate_limit = None

    def __repr__(self) -> str:
        """Representation of RenderInfo."""
        return f"<RenderInfo {self.template} all_states={self.all_states} all_states_lifecycle={self.all_states_lifecycle} domains={self.domains} domains_lifecycle={self.domains_lifecycle} entities={self.entities} rate_limit={self.rate_limit}>"

    def _filter_domains_and_entities(self, entity_id: str) -> bool:
        """Template should re-render if the entity state changes when we match specific domains or entities."""
        return (
            split_entity_id(entity_id)[0] in self.domains or entity_id in self.entities
        )

    def _filter_entities(self, entity_id: str) -> bool:
        """Template should re-render if the entity state changes when we match specific entities."""
        return entity_id in self.entities

    def _filter_lifecycle_domains(self, entity_id: str) -> bool:
        """Template should re-render if the entity is added or removed with domains watched."""
        return split_entity_id(entity_id)[0] in self.domains_lifecycle

    def result(self) -> str:
        """Results of the template computation."""
        if self.exception is not None:
            raise self.exception
        return self._result

    def _freeze_static(self) -> None:
        self.is_static = True
        self._freeze_sets()
        self.all_states = False

    def _freeze_sets(self) -> None:
        self.entities = frozenset(self.entities)
        self.domains = frozenset(self.domains)
        self.domains_lifecycle = frozenset(self.domains_lifecycle)

    def _freeze(self) -> None:
        self._freeze_sets()

        if self.rate_limit is None and (
            self.domains or self.domains_lifecycle or self.all_states or self.exception
        ):
            # If the template accesses all states or an entire
            # domain, and no rate limit is set, we use the default.
            self.rate_limit = DEFAULT_RATE_LIMIT

        if self.exception:
            return

        if not self.all_states_lifecycle:
            if self.domains_lifecycle:
                self.filter_lifecycle = self._filter_lifecycle_domains
            else:
                self.filter_lifecycle = _false

        if self.all_states:
            return

        if self.domains:
            self.filter = self._filter_domains_and_entities
        elif self.entities:
            self.filter = self._filter_entities
        else:
            self.filter = _false


class Template:
    """Class to hold a template and manage caching and rendering."""

    __slots__ = (
        "__weakref__",
        "template",
        "hass",
        "is_static",
        "_compiled_code",
        "_compiled",
    )

    def __init__(self, template, hass=None):
        """Instantiate a template."""
        if not isinstance(template, str):
            raise TypeError("Expected template to be a string")

        self.template: str = template
        self._compiled_code = None
        self._compiled = None
        self.hass = hass
        self.is_static = not is_template_string(template)

    @property
    def _env(self):
        if self.hass is None:
            return _NO_HASS_ENV
        ret = self.hass.data.get(_ENVIRONMENT)
        if ret is None:
            ret = self.hass.data[_ENVIRONMENT] = TemplateEnvironment(self.hass)
        return ret

    def ensure_valid(self):
        """Return if template is valid."""
        if self._compiled_code is not None:
            return

        try:
            self._compiled_code = self._env.compile(self.template)
        except jinja2.TemplateError as err:
            raise TemplateError(err) from err

    def extract_entities(
        self, variables: TemplateVarsType = None
    ) -> Union[str, List[str]]:
        """Extract all entities for state_changed listener."""
        if self.is_static:
            return []

        return extract_entities(self.hass, self.template, variables)

    def render(self, variables: TemplateVarsType = None, **kwargs: Any) -> Any:
        """Render given template."""
        if self.is_static:
            return self.template.strip()

        if variables is not None:
            kwargs.update(variables)

        return run_callback_threadsafe(
            self.hass.loop, self.async_render, kwargs
        ).result()

    @callback
    def async_render(self, variables: TemplateVarsType = None, **kwargs: Any) -> Any:
        """Render given template.

        This method must be run in the event loop.
        """
        if self.is_static:
            return self.template.strip()

        compiled = self._compiled or self._ensure_compiled()

        if variables is not None:
            kwargs.update(variables)

        try:
            render_result = compiled.render(kwargs)
        except Exception as err:  # pylint: disable=broad-except
            raise TemplateError(err) from err

        render_result = render_result.strip()

        if not self.hass.config.legacy_templates:
            try:
                result = literal_eval(render_result)

                # If the literal_eval result is a string, use the original
                # render, by not returning right here. The evaluation of strings
                # resulting in strings impacts quotes, to avoid unexpected
                # output; use the original render instead of the evaluated one.
                if not isinstance(result, str):
                    return result
            except (ValueError, SyntaxError, MemoryError):
                pass

        return render_result

    async def async_render_will_timeout(
        self, timeout: float, variables: TemplateVarsType = None, **kwargs: Any
    ) -> bool:
        """Check to see if rendering a template will timeout during render.

        This is intended to check for expensive templates
        that will make the system unstable.  The template
        is rendered in the executor to ensure it does not
        tie up the event loop.

        This function is not a security control and is only
        intended to be used as a safety check when testing
        templates.

        This method must be run in the event loop.
        """
        assert self.hass

        if self.is_static:
            return False

        compiled = self._compiled or self._ensure_compiled()

        if variables is not None:
            kwargs.update(variables)

        finish_event = asyncio.Event()

        def _render_template():
            try:
                compiled.render(kwargs)
            except TimeoutError:
                pass
            finally:
                run_callback_threadsafe(self.hass.loop, finish_event.set)

        try:
            template_render_thread = ThreadWithException(target=_render_template)
            template_render_thread.start()
            await asyncio.wait_for(finish_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            template_render_thread.raise_exc(TimeoutError)
            return True
        finally:
            template_render_thread.join()

        return False

    @callback
    def async_render_to_info(
        self, variables: TemplateVarsType = None, **kwargs: Any
    ) -> RenderInfo:
        """Render the template and collect an entity filter."""
        assert self.hass and _RENDER_INFO not in self.hass.data

        render_info = RenderInfo(self)

        # pylint: disable=protected-access
        if self.is_static:
            render_info._result = self.template.strip()
            render_info._freeze_static()
            return render_info

        self.hass.data[_RENDER_INFO] = render_info
        try:
            render_info._result = self.async_render(variables, **kwargs)
        except TemplateError as ex:
            render_info.exception = ex
        finally:
            del self.hass.data[_RENDER_INFO]

        render_info._freeze()
        return render_info

    def render_with_possible_json_value(self, value, error_value=_SENTINEL):
        """Render template with value exposed.

        If valid JSON will expose value_json too.
        """
        if self.is_static:
            return self.template

        return run_callback_threadsafe(
            self.hass.loop,
            self.async_render_with_possible_json_value,
            value,
            error_value,
        ).result()

    @callback
    def async_render_with_possible_json_value(
        self, value, error_value=_SENTINEL, variables=None
    ):
        """Render template with value exposed.

        If valid JSON will expose value_json too.

        This method must be run in the event loop.
        """
        if self.is_static:
            return self.template

        if self._compiled is None:
            self._ensure_compiled()

        variables = dict(variables or {})
        variables["value"] = value

        try:
            variables["value_json"] = json.loads(value)
        except (ValueError, TypeError):
            pass

        try:
            return self._compiled.render(variables).strip()
        except jinja2.TemplateError as ex:
            if error_value is _SENTINEL:
                _LOGGER.error(
                    "Error parsing value: %s (value: %s, template: %s)",
                    ex,
                    value,
                    self.template,
                )
            return value if error_value is _SENTINEL else error_value

    def _ensure_compiled(self):
        """Bind a template to a specific hass instance."""
        self.ensure_valid()

        assert self.hass is not None, "hass variable not set on template"

        env = self._env

        self._compiled = jinja2.Template.from_code(
            env, self._compiled_code, env.globals, None
        )

        return self._compiled

    def __eq__(self, other):
        """Compare template with another."""
        return (
            self.__class__ == other.__class__
            and self.template == other.template
            and self.hass == other.hass
        )

    def __hash__(self) -> int:
        """Hash code for template."""
        return hash(self.template)

    def __repr__(self) -> str:
        """Representation of Template."""
        return 'Template("' + self.template + '")'


class AllStates:
    """Class to expose all HA states as attributes."""

    def __init__(self, hass):
        """Initialize all states."""
        self._hass = hass

    def __getattr__(self, name):
        """Return the domain state."""
        if "." in name:
            return _get_state_if_valid(self._hass, name)

        if name in _RESERVED_NAMES:
            return None

        if not valid_entity_id(f"{name}.entity"):
            raise TemplateError(f"Invalid domain name '{name}'")

        return DomainStates(self._hass, name)

    # Jinja will try __getitem__ first and it avoids the need
    # to call is_safe_attribute
    __getitem__ = __getattr__

    def _collect_all(self) -> None:
        render_info = self._hass.data.get(_RENDER_INFO)
        if render_info is not None:
            render_info.all_states = True

    def _collect_all_lifecycle(self) -> None:
        render_info = self._hass.data.get(_RENDER_INFO)
        if render_info is not None:
            render_info.all_states_lifecycle = True

    def __iter__(self):
        """Return all states."""
        self._collect_all()
        return _state_generator(self._hass, None)

    def __len__(self) -> int:
        """Return number of states."""
        self._collect_all_lifecycle()
        return self._hass.states.async_entity_ids_count()

    def __call__(self, entity_id):
        """Return the states."""
        state = _get_state(self._hass, entity_id)
        return STATE_UNKNOWN if state is None else state.state

    def __repr__(self) -> str:
        """Representation of All States."""
        return "<template AllStates>"


class DomainStates:
    """Class to expose a specific HA domain as attributes."""

    def __init__(self, hass, domain):
        """Initialize the domain states."""
        self._hass = hass
        self._domain = domain

    def __getattr__(self, name):
        """Return the states."""
        return _get_state_if_valid(self._hass, f"{self._domain}.{name}")

    # Jinja will try __getitem__ first and it avoids the need
    # to call is_safe_attribute
    __getitem__ = __getattr__

    def _collect_domain(self) -> None:
        entity_collect = self._hass.data.get(_RENDER_INFO)
        if entity_collect is not None:
            entity_collect.domains.add(self._domain)

    def _collect_domain_lifecycle(self) -> None:
        entity_collect = self._hass.data.get(_RENDER_INFO)
        if entity_collect is not None:
            entity_collect.domains_lifecycle.add(self._domain)

    def __iter__(self):
        """Return the iteration over all the states."""
        self._collect_domain()
        return _state_generator(self._hass, self._domain)

    def __len__(self) -> int:
        """Return number of states."""
        self._collect_domain_lifecycle()
        return self._hass.states.async_entity_ids_count(self._domain)

    def __repr__(self) -> str:
        """Representation of Domain States."""
        return f"<template DomainStates('{self._domain}')>"


class TemplateState(State):
    """Class to represent a state object in a template."""

    __slots__ = ("_hass", "_state", "_collect")

    # Inheritance is done so functions that check against State keep working
    # pylint: disable=super-init-not-called
    def __init__(self, hass, state, collect=True):
        """Initialize template state."""
        self._hass = hass
        self._state = state
        self._collect = collect

    def _collect_state(self):
        if self._collect and _RENDER_INFO in self._hass.data:
            self._hass.data[_RENDER_INFO].entities.add(self._state.entity_id)

    # Jinja will try __getitem__ first and it avoids the need
    # to call is_safe_attribute
    def __getitem__(self, item):
        """Return a property as an attribute for jinja."""
        if item in _COLLECTABLE_STATE_ATTRIBUTES:
            # _collect_state inlined here for performance
            if self._collect and _RENDER_INFO in self._hass.data:
                self._hass.data[_RENDER_INFO].entities.add(self._state.entity_id)
            return getattr(self._state, item)
        if item == "entity_id":
            return self._state.entity_id
        if item == "state_with_unit":
            return self.state_with_unit
        raise KeyError

    @property
    def entity_id(self):
        """Wrap State.entity_id.

        Intentionally does not collect state
        """
        return self._state.entity_id

    @property
    def state(self):
        """Wrap State.state."""
        self._collect_state()
        return self._state.state

    @property
    def attributes(self):
        """Wrap State.attributes."""
        self._collect_state()
        return self._state.attributes

    @property
    def last_changed(self):
        """Wrap State.last_changed."""
        self._collect_state()
        return self._state.last_changed

    @property
    def last_updated(self):
        """Wrap State.last_updated."""
        self._collect_state()
        return self._state.last_updated

    @property
    def context(self):
        """Wrap State.context."""
        self._collect_state()
        return self._state.context

    @property
    def domain(self):
        """Wrap State.domain."""
        self._collect_state()
        return self._state.domain

    @property
    def object_id(self):
        """Wrap State.object_id."""
        self._collect_state()
        return self._state.object_id

    @property
    def name(self):
        """Wrap State.name."""
        self._collect_state()
        return self._state.name

    @property
    def state_with_unit(self) -> str:
        """Return the state concatenated with the unit if available."""
        self._collect_state()
        unit = self._state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        return f"{self._state.state} {unit}" if unit else self._state.state

    def __eq__(self, other: Any) -> bool:
        """Ensure we collect on equality check."""
        self._collect_state()
        return self._state.__eq__(other)

    def __repr__(self) -> str:
        """Representation of Template State."""
        return f"<template TemplateState({self._state.__repr__()})>"


def _collect_state(hass: HomeAssistantType, entity_id: str) -> None:
    entity_collect = hass.data.get(_RENDER_INFO)
    if entity_collect is not None:
        entity_collect.entities.add(entity_id)


def _state_generator(hass: HomeAssistantType, domain: Optional[str]) -> Generator:
    """State generator for a domain or all states."""
    for state in sorted(hass.states.async_all(domain), key=attrgetter("entity_id")):
        yield TemplateState(hass, state, collect=False)


def _get_state_if_valid(
    hass: HomeAssistantType, entity_id: str
) -> Optional[TemplateState]:
    state = hass.states.get(entity_id)
    if state is None and not valid_entity_id(entity_id):
        raise TemplateError(f"Invalid entity ID '{entity_id}'")  # type: ignore
    return _get_template_state_from_state(hass, entity_id, state)


def _get_state(hass: HomeAssistantType, entity_id: str) -> Optional[TemplateState]:
    return _get_template_state_from_state(hass, entity_id, hass.states.get(entity_id))


def _get_template_state_from_state(
    hass: HomeAssistantType, entity_id: str, state: Optional[State]
) -> Optional[TemplateState]:
    if state is None:
        # Only need to collect if none, if not none collect first actual
        # access to the state properties in the state wrapper.
        _collect_state(hass, entity_id)
        return None
    return TemplateState(hass, state)


def _resolve_state(
    hass: HomeAssistantType, entity_id_or_state: Any
) -> Union[State, TemplateState, None]:
    """Return state or entity_id if given."""
    if isinstance(entity_id_or_state, State):
        return entity_id_or_state
    if isinstance(entity_id_or_state, str):
        return _get_state(hass, entity_id_or_state)
    return None


def result_as_boolean(template_result: Optional[str]) -> bool:
    """Convert the template result to a boolean.

    True/not 0/'1'/'true'/'yes'/'on'/'enable' are considered truthy
    False/0/None/'0'/'false'/'no'/'off'/'disable' are considered falsy

    """
    try:
        return cv.boolean(template_result)
    except vol.Invalid:
        return False


def expand(hass: HomeAssistantType, *args: Any) -> Iterable[State]:
    """Expand out any groups into entity states."""
    search = list(args)
    found = {}
    while search:
        entity = search.pop()
        if isinstance(entity, str):
            entity_id = entity
            entity = _get_state(hass, entity)
            if entity is None:
                continue
        elif isinstance(entity, State):
            entity_id = entity.entity_id
        elif isinstance(entity, collections.abc.Iterable):
            search += entity
            continue
        else:
            # ignore other types
            continue

        if entity_id.startswith(_GROUP_DOMAIN_PREFIX):
            # Collect state will be called in here since it's wrapped
            group_entities = entity.attributes.get(ATTR_ENTITY_ID)
            if group_entities:
                search += group_entities
        else:
            _collect_state(hass, entity_id)
            found[entity_id] = entity

    return sorted(found.values(), key=lambda a: a.entity_id)


def closest(hass, *args):
    """Find closest entity.

    Closest to home:
        closest(states)
        closest(states.device_tracker)
        closest('group.children')
        closest(states.group.children)

    Closest to a point:
        closest(23.456, 23.456, 'group.children')
        closest('zone.school', 'group.children')
        closest(states.zone.school, 'group.children')

    As a filter:
        states | closest
        states.device_tracker | closest
        ['group.children', states.device_tracker] | closest
        'group.children' | closest(23.456, 23.456)
        states.device_tracker | closest('zone.school')
        'group.children' | closest(states.zone.school)

    """
    if len(args) == 1:
        latitude = hass.config.latitude
        longitude = hass.config.longitude
        entities = args[0]

    elif len(args) == 2:
        point_state = _resolve_state(hass, args[0])

        if point_state is None:
            _LOGGER.warning("Closest:Unable to find state %s", args[0])
            return None
        if not loc_helper.has_location(point_state):
            _LOGGER.warning(
                "Closest:State does not contain valid location: %s", point_state
            )
            return None

        latitude = point_state.attributes.get(ATTR_LATITUDE)
        longitude = point_state.attributes.get(ATTR_LONGITUDE)

        entities = args[1]

    else:
        latitude = convert(args[0], float)
        longitude = convert(args[1], float)

        if latitude is None or longitude is None:
            _LOGGER.warning(
                "Closest:Received invalid coordinates: %s, %s", args[0], args[1]
            )
            return None

        entities = args[2]

    states = expand(hass, entities)

    # state will already be wrapped here
    return loc_helper.closest(latitude, longitude, states)


def closest_filter(hass, *args):
    """Call closest as a filter. Need to reorder arguments."""
    new_args = list(args[1:])
    new_args.append(args[0])
    return closest(hass, *new_args)


def distance(hass, *args):
    """Calculate distance.

    Will calculate distance from home to a point or between points.
    Points can be passed in using state objects or lat/lng coordinates.
    """
    locations = []

    to_process = list(args)

    while to_process:
        value = to_process.pop(0)
        if isinstance(value, str) and not valid_entity_id(value):
            point_state = None
        else:
            point_state = _resolve_state(hass, value)

        if point_state is None:
            # We expect this and next value to be lat&lng
            if not to_process:
                _LOGGER.warning(
                    "Distance:Expected latitude and longitude, got %s", value
                )
                return None

            value_2 = to_process.pop(0)
            latitude = convert(value, float)
            longitude = convert(value_2, float)

            if latitude is None or longitude is None:
                _LOGGER.warning(
                    "Distance:Unable to process latitude and longitude: %s, %s",
                    value,
                    value_2,
                )
                return None

        else:
            if not loc_helper.has_location(point_state):
                _LOGGER.warning(
                    "distance:State does not contain valid location: %s", point_state
                )
                return None

            latitude = point_state.attributes.get(ATTR_LATITUDE)
            longitude = point_state.attributes.get(ATTR_LONGITUDE)

        locations.append((latitude, longitude))

    if len(locations) == 1:
        return hass.config.distance(*locations[0])

    return hass.config.units.length(
        loc_util.distance(*locations[0] + locations[1]), LENGTH_METERS
    )


def is_state(hass: HomeAssistantType, entity_id: str, state: State) -> bool:
    """Test if a state is a specific value."""
    state_obj = _get_state(hass, entity_id)
    return state_obj is not None and state_obj.state == state


def is_state_attr(hass, entity_id, name, value):
    """Test if a state's attribute is a specific value."""
    attr = state_attr(hass, entity_id, name)
    return attr is not None and attr == value


def state_attr(hass, entity_id, name):
    """Get a specific attribute from a state."""
    state_obj = _get_state(hass, entity_id)
    if state_obj is not None:
        return state_obj.attributes.get(name)
    return None


def forgiving_round(value, precision=0, method="common"):
    """Round accepted strings."""
    try:
        # support rounding methods like jinja
        multiplier = float(10 ** precision)
        if method == "ceil":
            value = math.ceil(float(value) * multiplier) / multiplier
        elif method == "floor":
            value = math.floor(float(value) * multiplier) / multiplier
        elif method == "half":
            value = round(float(value) * 2) / 2
        else:
            # if method is common or something else, use common rounding
            value = round(float(value), precision)
        return int(value) if precision == 0 else value
    except (ValueError, TypeError):
        # If value can't be converted to float
        return value


def multiply(value, amount):
    """Filter to convert value to float and multiply it."""
    try:
        return float(value) * amount
    except (ValueError, TypeError):
        # If value can't be converted to float
        return value


def logarithm(value, base=math.e):
    """Filter to get logarithm of the value with a specific base."""
    try:
        return math.log(float(value), float(base))
    except (ValueError, TypeError):
        return value


def sine(value):
    """Filter to get sine of the value."""
    try:
        return math.sin(float(value))
    except (ValueError, TypeError):
        return value


def cosine(value):
    """Filter to get cosine of the value."""
    try:
        return math.cos(float(value))
    except (ValueError, TypeError):
        return value


def tangent(value):
    """Filter to get tangent of the value."""
    try:
        return math.tan(float(value))
    except (ValueError, TypeError):
        return value


def arc_sine(value):
    """Filter to get arc sine of the value."""
    try:
        return math.asin(float(value))
    except (ValueError, TypeError):
        return value


def arc_cosine(value):
    """Filter to get arc cosine of the value."""
    try:
        return math.acos(float(value))
    except (ValueError, TypeError):
        return value


def arc_tangent(value):
    """Filter to get arc tangent of the value."""
    try:
        return math.atan(float(value))
    except (ValueError, TypeError):
        return value


def arc_tangent2(*args):
    """Filter to calculate four quadrant arc tangent of y / x."""
    try:
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            args = args[0]

        return math.atan2(float(args[0]), float(args[1]))
    except (ValueError, TypeError):
        return args


def square_root(value):
    """Filter to get square root of the value."""
    try:
        return math.sqrt(float(value))
    except (ValueError, TypeError):
        return value


def timestamp_custom(value, date_format=DATE_STR_FORMAT, local=True):
    """Filter to convert given timestamp to format."""
    try:
        date = dt_util.utc_from_timestamp(value)

        if local:
            date = dt_util.as_local(date)

        return date.strftime(date_format)
    except (ValueError, TypeError):
        # If timestamp can't be converted
        return value


def timestamp_local(value):
    """Filter to convert given timestamp to local date/time."""
    try:
        return dt_util.as_local(dt_util.utc_from_timestamp(value)).strftime(
            DATE_STR_FORMAT
        )
    except (ValueError, TypeError):
        # If timestamp can't be converted
        return value


def timestamp_utc(value):
    """Filter to convert given timestamp to UTC date/time."""
    try:
        return dt_util.utc_from_timestamp(value).strftime(DATE_STR_FORMAT)
    except (ValueError, TypeError):
        # If timestamp can't be converted
        return value


def forgiving_as_timestamp(value):
    """Try to convert value to timestamp."""
    try:
        return dt_util.as_timestamp(value)
    except (ValueError, TypeError):
        return None


def strptime(string, fmt):
    """Parse a time string to datetime."""
    try:
        return datetime.strptime(string, fmt)
    except (ValueError, AttributeError, TypeError):
        return string


def fail_when_undefined(value):
    """Filter to force a failure when the value is undefined."""
    if isinstance(value, jinja2.Undefined):
        value()
    return value


def forgiving_float(value):
    """Try to convert value to a float."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return value


def regex_match(value, find="", ignorecase=False):
    """Match value using regex."""
    if not isinstance(value, str):
        value = str(value)
    flags = re.I if ignorecase else 0
    return bool(re.match(find, value, flags))


def regex_replace(value="", find="", replace="", ignorecase=False):
    """Replace using regex."""
    if not isinstance(value, str):
        value = str(value)
    flags = re.I if ignorecase else 0
    regex = re.compile(find, flags)
    return regex.sub(replace, value)


def regex_search(value, find="", ignorecase=False):
    """Search using regex."""
    if not isinstance(value, str):
        value = str(value)
    flags = re.I if ignorecase else 0
    return bool(re.search(find, value, flags))


def regex_findall_index(value, find="", index=0, ignorecase=False):
    """Find all matches using regex and then pick specific match index."""
    if not isinstance(value, str):
        value = str(value)
    flags = re.I if ignorecase else 0
    return re.findall(find, value, flags)[index]


def bitwise_and(first_value, second_value):
    """Perform a bitwise and operation."""
    return first_value & second_value


def bitwise_or(first_value, second_value):
    """Perform a bitwise or operation."""
    return first_value | second_value


def base64_encode(value):
    """Perform base64 encode."""
    return base64.b64encode(value.encode("utf-8")).decode("utf-8")


def base64_decode(value):
    """Perform base64 denode."""
    return base64.b64decode(value).decode("utf-8")


def ordinal(value):
    """Perform ordinal conversion."""
    return str(value) + (
        list(["th", "st", "nd", "rd"] + ["th"] * 6)[(int(str(value)[-1])) % 10]
        if int(str(value)[-2:]) % 100 not in range(11, 14)
        else "th"
    )


def from_json(value):
    """Convert a JSON string to an object."""
    return json.loads(value)


def to_json(value):
    """Convert an object to a JSON string."""
    return json.dumps(value)


@contextfilter
def random_every_time(context, values):
    """Choose a random value.

    Unlike Jinja's random filter,
    this is context-dependent to avoid caching the chosen value.
    """
    return random.choice(values)


def relative_time(value):
    """
    Take a datetime and return its "age" as a string.

    The age can be in second, minute, hour, day, month or year. Only the
    biggest unit is considered, e.g. if it's 2 days and 3 hours, "2 days" will
    be returned.
    Make sure date is not in the future, or else it will return None.

    If the input are not a datetime object the input will be returned unmodified.
    """

    if not isinstance(value, datetime):
        return value
    if not value.tzinfo:
        value = dt_util.as_local(value)
    if dt_util.now() < value:
        return value
    return dt_util.get_age(value)


def urlencode(value):
    """Urlencode dictionary and return as UTF-8 string."""
    return urllib_urlencode(value).encode("utf-8")


class TemplateEnvironment(ImmutableSandboxedEnvironment):
    """The Home Assistant template environment."""

    def __init__(self, hass):
        """Initialise template environment."""
        super().__init__()
        self.hass = hass
        self.template_cache = weakref.WeakValueDictionary()
        self.filters["round"] = forgiving_round
        self.filters["multiply"] = multiply
        self.filters["log"] = logarithm
        self.filters["sin"] = sine
        self.filters["cos"] = cosine
        self.filters["tan"] = tangent
        self.filters["asin"] = arc_sine
        self.filters["acos"] = arc_cosine
        self.filters["atan"] = arc_tangent
        self.filters["atan2"] = arc_tangent2
        self.filters["sqrt"] = square_root
        self.filters["as_timestamp"] = forgiving_as_timestamp
        self.filters["as_local"] = dt_util.as_local
        self.filters["timestamp_custom"] = timestamp_custom
        self.filters["timestamp_local"] = timestamp_local
        self.filters["timestamp_utc"] = timestamp_utc
        self.filters["to_json"] = to_json
        self.filters["from_json"] = from_json
        self.filters["is_defined"] = fail_when_undefined
        self.filters["max"] = max
        self.filters["min"] = min
        self.filters["random"] = random_every_time
        self.filters["base64_encode"] = base64_encode
        self.filters["base64_decode"] = base64_decode
        self.filters["ordinal"] = ordinal
        self.filters["regex_match"] = regex_match
        self.filters["regex_replace"] = regex_replace
        self.filters["regex_search"] = regex_search
        self.filters["regex_findall_index"] = regex_findall_index
        self.filters["bitwise_and"] = bitwise_and
        self.filters["bitwise_or"] = bitwise_or
        self.filters["ord"] = ord
        self.globals["log"] = logarithm
        self.globals["sin"] = sine
        self.globals["cos"] = cosine
        self.globals["tan"] = tangent
        self.globals["sqrt"] = square_root
        self.globals["pi"] = math.pi
        self.globals["tau"] = math.pi * 2
        self.globals["e"] = math.e
        self.globals["asin"] = arc_sine
        self.globals["acos"] = arc_cosine
        self.globals["atan"] = arc_tangent
        self.globals["atan2"] = arc_tangent2
        self.globals["float"] = forgiving_float
        self.globals["now"] = dt_util.now
        self.globals["as_local"] = dt_util.as_local
        self.globals["utcnow"] = dt_util.utcnow
        self.globals["as_timestamp"] = forgiving_as_timestamp
        self.globals["relative_time"] = relative_time
        self.globals["timedelta"] = timedelta
        self.globals["strptime"] = strptime
        self.globals["urlencode"] = urlencode
        if hass is None:
            return

        # We mark these as a context functions to ensure they get
        # evaluated fresh with every execution, rather than executed
        # at compile time and the value stored. The context itself
        # can be discarded, we only need to get at the hass object.
        def hassfunction(func):
            """Wrap function that depend on hass."""

            @wraps(func)
            def wrapper(*args, **kwargs):
                return func(hass, *args[1:], **kwargs)

            return contextfunction(wrapper)

        self.globals["expand"] = hassfunction(expand)
        self.filters["expand"] = contextfilter(self.globals["expand"])
        self.globals["closest"] = hassfunction(closest)
        self.filters["closest"] = contextfilter(hassfunction(closest_filter))
        self.globals["distance"] = hassfunction(distance)
        self.globals["is_state"] = hassfunction(is_state)
        self.globals["is_state_attr"] = hassfunction(is_state_attr)
        self.globals["state_attr"] = hassfunction(state_attr)
        self.globals["states"] = AllStates(hass)

    def is_safe_callable(self, obj):
        """Test if callback is safe."""
        return isinstance(obj, AllStates) or super().is_safe_callable(obj)

    def is_safe_attribute(self, obj, attr, value):
        """Test if attribute is safe."""
        if isinstance(obj, (AllStates, DomainStates, TemplateState)):
            return not attr[0] == "_"

        if isinstance(obj, Namespace):
            return True

        return super().is_safe_attribute(obj, attr, value)

    def compile(self, source, name=None, filename=None, raw=False, defer_init=False):
        """Compile the template."""
        if (
            name is not None
            or filename is not None
            or raw is not False
            or defer_init is not False
        ):
            # If there are any non-default keywords args, we do
            # not cache.  In prodution we currently do not have
            # any instance of this.
            return super().compile(source, name, filename, raw, defer_init)

        cached = self.template_cache.get(source)

        if cached is None:
            cached = self.template_cache[source] = super().compile(source)

        return cached


_NO_HASS_ENV = TemplateEnvironment(None)
