from aiogram.fsm.state import State, StatesGroup


class ReminderFlow(StatesGroup):
    waiting_time = State()
    waiting_custom_offset = State()
    choosing_offsets = State()
    waiting_reschedule = State()


class PersonFlow(StatesGroup):
    confirm_create = State()
    confirm_add_existing = State()
    disambiguate = State()
    waiting_name_edit = State()
    waiting_new_note = State()
    waiting_rename = State()
    confirm_delete_person = State()
