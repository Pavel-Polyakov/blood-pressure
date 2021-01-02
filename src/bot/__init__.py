import datetime
import os
import time
from contextlib import contextmanager
from threading import Thread

import telebot
from loguru import logger
from tinydb import Query
from transitions import MachineError

import dispatchers as d
import models as m
from db import db

bot = telebot.TeleBot(os.getenv("TELEGRAM_BOT_TOKEN"))


@contextmanager
def error_handler(chat_id):
    try:
        yield
    except MachineError:
        logger.exception(f'exception when handling message from {chat_id}')
        bot.send_message(chat_id, "Неправильное состояние. Проверьте сообщение и попробуйте ещё раз")
    except Exception:
        logger.exception(f'exception when handling message from {chat_id}')
        bot.send_message(chat_id, "Кажется, здесь какая-то ошибка, давайте попробуем ещё раз")


def get_user_query(chat_id) -> Query:
    return Query().chat_id == chat_id  # noqa


def _get_user(message: telebot.types.Message):
    result = db.table("users").search(get_user_query(message.chat.id))
    if not result:
        return None
    return m.User(**result[0])


def _check_state(message: telebot.types.Message, state: m.State) -> bool:
    user = _get_user(message)
    return user and user.state == state


def handle(message, state):
    chat_id = message.chat.id

    with error_handler(chat_id=chat_id):
        result = db.table("users").search(get_user_query(chat_id))

        if result:
            user = m.User(**result[0])
        else:
            user = m.User.new(chat_id)

        d.UserDispatcher(
            user,
            message,
            bot
        ).transit(state)

        db.table("users").upsert(user.dict(), get_user_query(chat_id))


@bot.message_handler(commands=['старт', 'start'])
def handle_start(message: telebot.types.Message):
    handle(message, m.State.START)


@bot.message_handler(commands=['стоп', 'stop'])
def handle_stop(message: telebot.types.Message):
    handle(message, m.State.STOP)


@bot.message_handler(commands=['история', 'history'])
def handle_history(message: telebot.types.Message):
    if _get_user(message).tz is None:
        handle(message, m.State.WAIT_FOR_TZ_HISTORY)
    else:
        handle(message, m.State.HISTORY)


@bot.message_handler(commands=['когда', 'when'])
def handle_when(message: telebot.types.Message):
    if _get_user(message).tz is None:
        handle(message, m.State.WAIT_FOR_TZ_WHEN)
    else:
        handle(message, m.State.WAIT_FOR_MORNING_TIME)


@bot.message_handler(commands=['где', 'where'])
def handle_where(message: telebot.types.Message):
    handle(message, m.State.WAIT_FOR_TZ)


@bot.message_handler(commands=['статус', 'status'])
def handle_status(message: telebot.types.Message):
    handle(message, m.State.STATUS)


@bot.message_handler(regexp=r'\d\d\:\d\d', func=lambda msg: _check_state(msg, m.State.WAIT_FOR_MORNING_TIME))
def handle_time_morning(message: telebot.types.Message):
    handle(message, m.State.WAIT_FOR_EVENING_TIME)


@bot.message_handler(regexp=r'\d\d\:\d\d', func=lambda msg: _check_state(msg, m.State.WAIT_FOR_EVENING_TIME))
def handle_time_evening(message: telebot.types.Message):
    handle(message, m.State.WAIT)


@bot.message_handler(regexp=r'\d{1,3}\/\d{1,3}', func=lambda msg: _check_state(msg, m.State.WAIT))
def handle_record(message: telebot.types.Message):
    handle(message, m.State.RECORD)


@bot.message_handler(func=lambda msg: _check_state(msg, m.State.WAIT_FOR_TZ))
def handle_message(message: telebot.types.Message):
    handle(message, m.State.WAIT)


@bot.message_handler(func=lambda msg: _check_state(msg, m.State.WAIT_FOR_TZ_WHEN))
def handle_message(message: telebot.types.Message):
    handle(message, m.State.WAIT_FOR_MORNING_TIME)


@bot.message_handler(func=lambda msg: _check_state(msg, m.State.WAIT_FOR_TZ_HISTORY))
def handle_message(message: telebot.types.Message):
    handle(message, m.State.HISTORY)


@bot.message_handler()
def handle_message(message: telebot.types.Message):
    bot.send_message(message.chat.id, f'🤔')


def worker():
    start_time = datetime.datetime.now().replace(second=0, microsecond=0).timestamp()

    while True:
        logger.info(f'starting worker cycle')
        for user in [m.User(**x) for x in db.table("users").all()]:
            with error_handler(chat_id=user.chat_id):
                if user.reminder_morning and user.reminder_morning.is_notify_needed():
                    logger.info(f'notifying morning {user}')
                    d.UserDispatcher(user, None, bot).transit(m.State.NOTIFY_MORNING)
                    db.table("users").upsert(user.dict(), get_user_query(user.chat_id))

                if user.reminder_evening and user.reminder_evening.is_notify_needed():
                    logger.info(f'notifying evening {user}')
                    d.UserDispatcher(user, None, bot).transit(m.State.NOTIFY_EVENING)
                    db.table("users").upsert(user.dict(), get_user_query(user.chat_id))

                if user.reminder_forgot and user.reminder_forgot.is_notify_needed():
                    logger.info(f'notify forgot {user}')
                    d.UserDispatcher(user, None, bot).transit(m.State.NOTIFY_FORGOT)
                    db.table("users").upsert(user.dict(), get_user_query(user.chat_id))

        time.sleep(60.0 - ((time.time() - start_time) % 60.0))


def start():
    Thread(target=worker, daemon=True).start()
    bot.polling()
