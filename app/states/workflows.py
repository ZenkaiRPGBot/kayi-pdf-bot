"""FSM states for the five workflows."""

from aiogram.fsm.state import State, StatesGroup


class MergeStates(StatesGroup):
    collecting = State()


class SplitStates(StatesGroup):
    waiting_file = State()
    waiting_ranges = State()


class ExtractStates(StatesGroup):
    waiting_file = State()
    waiting_pages = State()


class RotateStates(StatesGroup):
    waiting_file = State()
    choosing = State()
    waiting_pages = State()


class ImagesStates(StatesGroup):
    collecting = State()
    renaming = State()


class UploadChoiceStates(StatesGroup):
    choosing = State()
