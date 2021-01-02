import datetime

import telebot
from loguru import logger
from transitions import Machine

import models as m

transitions = [
    ["to_start", m.State.INITIAL, m.State.START],
    ["to_wait", '*', m.State.WAIT],
    ["to_wait_for_tz", [m.State.STOP, m.State.WAIT], m.State.WAIT_FOR_TZ],
    ["to_wait_for_tz_when", [m.State.STOP, m.State.WAIT], m.State.WAIT_FOR_TZ_WHEN],
    ["to_wait_for_tz_history", [m.State.STOP, m.State.WAIT], m.State.WAIT_FOR_TZ_HISTORY],
    ["to_wait_for_morning_time", [m.State.STOP, m.State.WAIT, m.State.WAIT_FOR_TZ_WHEN], m.State.WAIT_FOR_MORNING_TIME],
    ["to_wait_for_evening_time", m.State.WAIT_FOR_MORNING_TIME, m.State.WAIT_FOR_EVENING_TIME],
    ["to_record", m.State.WAIT, m.State.RECORD],
    ["to_stop", '*', m.State.STOP],
    ["to_history", [m.State.STOP, m.State.WAIT, m.State.WAIT_FOR_TZ_HISTORY], m.State.HISTORY],
    ["to_notify_morning", m.State.WAIT, m.State.NOTIFY_MORNING],
    ["to_notify_evening", m.State.WAIT, m.State.NOTIFY_EVENING],
    ["to_notify_forgot", m.State.WAIT, m.State.NOTIFY_FORGOT],
    ["to_status", [m.State.STOP, m.State.WAIT], m.State.STATUS]
]


class UserDispatcher(Machine):
    def __init__(self, user: m.User, message: telebot.types.Message, bot: telebot.TeleBot):
        self.user = user
        self.message = message
        self.bot = bot
        super().__init__(states=m.State, transitions=transitions, initial=user.state)

    @property
    def messages(self):
        return m.Messages(self.user)

    def send(self, *messages):
        return self.bot.send_message(self.user.chat_id, '\n'.join(messages))

    def transit(self, state: m.State):
        logger.debug(f"transition to {state}")
        trigger = "to_" + state.value
        self.dispatch(trigger)
        self.user.state = self.state

    def on_enter_START(self):
        self.send(self.messages.starting)
        self.transit(m.State.WAIT)

    def on_enter_STOP(self):
        self.user.reminder_morning = None
        self.user.reminder_evening = None
        self.send(self.messages.stopped)

    def on_enter_HISTORY(self):
        self.send(self.messages.history)
        self.transit(m.State.WAIT)

    def on_enter_RECORD(self):
        measure = m.Measurement.from_string(self.message.text, timestamp=datetime.datetime.utcnow().timestamp())
        self.user.measurements.append(measure)
        self.user.reminder_forgot.reset()
        self.send(self.messages.thanks_record)
        self.transit(m.State.WAIT)

    def on_enter_STATUS(self):
        self.send(self.messages.reminders)
        self.transit(m.State.WAIT)

    def on_enter_NOTIFY_MORNING(self):
        self.user.reminder_morning.update_next_time()
        self.send(self.messages.notify_morning)
        self.user.reminder_forgot.update()
        self.transit(m.State.WAIT)

    def on_enter_NOTIFY_EVENING(self):
        self.user.reminder_evening.update_next_time()
        self.send(self.messages.notify_evening)
        self.user.reminder_forgot.update()
        self.transit(m.State.WAIT)

    def on_enter_NOTIFY_FORGOT(self):
        self.send(self.messages.notify_forgot)
        self.user.reminder_forgot.reset()
        self.transit(m.State.WAIT)

    def on_enter_WAIT_FOR_MORNING_TIME(self):
        self.send(self.messages.wait_for_morning_time)

    def on_exit_WAIT_FOR_MORNING_TIME(self):
        self.user.reminder_morning = m.Reminder.new(self.message.text, self.user.tz)

    def on_enter_WAIT_FOR_EVENING_TIME(self):
        self.send(self.messages.wait_for_evening_time)

    def on_exit_WAIT_FOR_EVENING_TIME(self):
        self.user.reminder_evening = m.Reminder.new(self.message.text, self.user.tz)
        self.user.reminder_forgot = m.ReminderForgot.new()
        self.send(self.messages.thanks, self.messages.reminders)

    def on_enter_WAIT_FOR_TZ(self):
        self.send(self.messages.wait_for_tz)

    def on_enter_WAIT_FOR_TZ_WHEN(self):
        self.send(self.messages.wait_for_tz_when)

    def on_enter_WAIT_FOR_TZ_HISTORY(self):
        self.send(self.messages.wait_for_tz_history)

    def on_exit_WAIT_FOR_TZ_WHEN(self):
        self.user.tz = m.TZ.from_city(self.message.text)
        self.send(self.messages.thanks, self.messages.tz)

    def on_exit_WAIT_FOR_TZ(self):
        self.user.tz = m.TZ.from_city(self.message.text)
        self.send(self.messages.thanks, self.messages.tz)

    def on_exit_WAIT_FOR_TZ_HISTORY(self):
        self.user.tz = m.TZ.from_city(self.message.text)
        self.send(self.messages.thanks, self.messages.tz)
