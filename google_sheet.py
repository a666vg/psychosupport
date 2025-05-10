"""
Взаимодействие с Google Sheets для записи к психологам
"""
from datetime import datetime, timedelta
from time import time
from threading import Lock
import json
from concurrent.futures import ThreadPoolExecutor
from google.oauth2.service_account import Credentials
from retrying import retry
from cachetools import TTLCache
import gspread
from pytz import timezone

myscope = ["https://www.googleapis.com/auth/spreadsheets",
           "https://www.googleapis.com/auth/drive"]

# Временная зона
tz = timezone("Europe/Moscow")
# Название файла JSON ключа (замените на ваше имя файла)
creds = Credentials.from_service_account_file('psychology.json', scopes=myscope)
client_main = gspread.Client(creds)
# Название таблицы (ID из URL)
sh = client_main.open_by_key('15viMBxMIO_J0JQ9COrN5lxbsf3bmzdcMRlyqtI54Yck')
# Страницы таблицы, которые должны игнорироваться
IGNOR_WORKSHEETS = ['Психологи']
# Страница таблицы с психологами и локациями
NAME_SHEET_PSYCHOLOGISTS = 'Психологи'
# Названия основных колонок (очередность важна!)
NAME_COL_LOCATION = 'Локация'
NAME_COL_PSYCHOLOGIST = 'Психолог'

# Кэш листов с TTL 12 часов
CACHE_WORKSHEETS = TTLCache(maxsize=2, ttl=12 * 60 * 60)
# Кэш доступных дат для локации-психолога с TTL 15 минут
CACHE_DAYS = TTLCache(maxsize=6, ttl=15 * 60)
# Lock для синхронизации доступа
lock = Lock()

def serialize_dict(dct: dict) -> str:
    """Сериализатор JSON"""
    return json.dumps(dct)

def deserialize_dict(json_str: str) -> dict:
    """Десериализатор JSON"""
    return json.loads(json_str)

def get_cache_days(location_name: str, psychologist_name: str) -> list | None:
    """
    Запрашивает свободные даты из кэша

    :param location_name: Название локации
    :param psychologist_name: Имя психолога
    """
    if location_name in CACHE_DAYS:
        cached_value = CACHE_DAYS[location_name]
        cached_dict = deserialize_dict(cached_value)
        if psychologist_name in cached_dict:
            return cached_dict[psychologist_name]
    return None

def update_cache_days(location_name: str, psychologist_name: str, available_dates: list) -> None:
    """
    Обновляет свободные даты в кэше

    :param location_name: Название локации
    :param psychologist_name: Имя психолога
    :param available_dates: Доступные даты
    """
    if psychologist_name is None:
        psychologist_name = 'null'
    if location_name in CACHE_DAYS:
        cached_value = CACHE_DAYS[location_name]
        cached_dict = deserialize_dict(cached_value)
        if psychologist_name not in cached_dict:
            cached_dict[psychologist_name] = available_dates
            cache_value = serialize_dict(cached_dict)
            CACHE_DAYS[location_name] = cache_value
    else:
        cache_value = serialize_dict({psychologist_name: available_dates})
        CACHE_DAYS[location_name] = cache_value

@retry(wait_exponential_multiplier=3000, wait_exponential_max=3000)
def get_sheet_names() -> list:
    """
    Запрашивает все имена листов таблицы
    """
    if 'worksheets' in CACHE_WORKSHEETS:
        return CACHE_WORKSHEETS['worksheets']
    with lock:
        worksheets = sh.worksheets()
    CACHE_WORKSHEETS['worksheets'] = worksheets
    return worksheets

@retry(wait_exponential_multiplier=3000, wait_exponential_max=3000)
def get_cache_locations() -> dict:
    """
    Запрашивает все локации и психологов из листа 'Психологи'
    """
    if 'locations' in CACHE_WORKSHEETS:
        return CACHE_WORKSHEETS['locations']
    dct = {}
    with lock:
        ws = sh.worksheet(NAME_SHEET_PSYCHOLOGISTS)
    for i in ws.get_all_records():
        location = i[NAME_COL_LOCATION].strip()
        psychologist = i[NAME_COL_PSYCHOLOGIST].strip()
        dct[location] = dct.get(location, [])
        dct[location].append(psychologist)
    CACHE_WORKSHEETS['locations'] = dct
    return dct

def time_score(func):
    """Декоратор для трекинга времени выполнения функции"""
    def wrapper(*args, **kwargs):
        start = time()
        res = func(*args, **kwargs)
        print(f"---{func.__name__} = %s seconds ---" % round(time() - start, 2))
        return res
    return wrapper

class GoogleSheets:
    """Взаимодействие с Google Sheets для записи к психологам"""
    def __init__(self, client_id: str):
        self.client_id = client_id
        self.lst_currant_date = None
        self.dct_currant_time = None
        self.lst_records = None
        self.location = None
        self.psychologist = None
        self.date_record = None
        self.time_record = None

    def __str__(self):
        return f'Инфо о клиенте:\n' \
               f'{self.client_id=}\n' \
               f'{self.location=}\n' \
               f'{self.psychologist=}\n' \
               f'{self.date_record=}\n' \
               f'{self.time_record=}'

    @retry(wait_exponential_multiplier=3000, wait_exponential_max=9000)
    def get_all_days(self) -> list:
        """Все доступные дни для записи на определенную локацию"""
        check = get_cache_days(self.location, self.psychologist)
        if check:
            return check

        @retry(wait_exponential_multiplier=3000, wait_exponential_max=3000)
        def actual_date(sheet_obj, count_days=7) -> bool:
            """
            Проверяет актуальные даты для записи на ближайшие count_days дней

            :param sheet_obj: Объект листа (gspread)
            :param count_days: Количество дней для поиска
            :return: Название листа (дата) или False
            """
            if sheet_obj.title in IGNOR_WORKSHEETS:
                return False
            try:
                date_sheet = datetime.strptime(sheet_obj.title.strip(), '%d.%m.%Y').date()
            except Exception as ex:
                print(ex, sheet_obj.title, '- Добавьте лист в IGNOR_WORKSHEETS')
                return False
            date_today = datetime.now(tz=tz)
            if not date_today.date() <= date_sheet <= (date_today.date() + timedelta(days=count_days)):
                return False
            with lock:
                val = sheet_obj.get_all_records()
            for dct in val:
                if date_today.date() == date_sheet:
                    if (self.psychologist is not None and
                        dct[NAME_COL_PSYCHOLOGIST].strip() == self.psychologist) or \
                       (self.psychologist is None):
                        for k, v in dct.items():
                            if str(v).strip() == '' and k == 'Клиент' and \
                               date_today.time() < datetime.strptime(dct['Время'], '%H:%M').time():
                                return sheet_obj.title
                    continue
                if (self.psychologist is not None and dct[NAME_COL_PSYCHOLOGIST].strip() == self.psychologist) \
                   or (self.psychologist is None):
                    for k, v in dct.items():
                        if str(v).strip() == '' and k == 'Клиент':
                            return sheet_obj.title
            return False

        worksheet_all = get_sheet_names()
        with ThreadPoolExecutor(2) as executor:
            res = executor.map(actual_date, worksheet_all)
            res = list(filter(lambda x: type(x) is str, res))
        update_cache_days(self.location, self.psychologist, res)
        return res

    @retry(wait_exponential_multiplier=3000, wait_exponential_max=3000)
    def get_free_time(self) -> list:
        """Выгружает свободное время для выбранной даты"""
        try:
            with lock:
                all_val = sh.worksheet(self.date_record).get_all_records()
        except gspread.exceptions.WorksheetNotFound as not_found:
            print(not_found, self.date_record, '- Дата занята/не найдена')
            return []
        if self.date_record == datetime.now(tz=tz).strftime('%d.%m.%Y'):
            lst = [i['Время'].strip() for i in all_val
                   if (self.psychologist is None or
                       i[NAME_COL_PSYCHOLOGIST].strip() == self.psychologist) and
                   i['Клиент'].strip() == '' and
                   datetime.now(tz=tz).time() < datetime.strptime(i['Время'], '%H:%M').time()]
        else:
            lst = [i['Время'].strip() for i in all_val
                   if (self.psychologist is None or
                       i[NAME_COL_PSYCHOLOGIST].strip() == self.psychologist) and
                   i['Клиент'].strip() == '']
        if len(lst) > 0:
            lst = sorted(list(set(lst)))
        return lst

    @retry(wait_exponential_multiplier=3000, wait_exponential_max=3000)
    def set_time(self, client_record='', search_criteria='') -> bool:
        """
        Записывает или отменяет запись клиента

        :param client_record: Данные клиента
        :param search_criteria: Критерий поиска ('', client_record)
        :return: True при успехе, False при ошибке
        """
        try:
            with lock:
                all_val = sh.worksheet(self.date_record).get_all_records()
        except gspread.exceptions.WorksheetNotFound as not_found:
            print(not_found, self.date_record, '- Дата занята/не найдена')
            return False
        row_num = 1
        for i in all_val:
            row_num += 1
            if (self.psychologist is None or
                i[NAME_COL_PSYCHOLOGIST].strip() == self.psychologist) and \
               i['Время'].strip() == self.time_record and \
               i['Клиент'].strip() == search_criteria:
                if self.psychologist is None:
                    self.psychologist = i[NAME_COL_PSYCHOLOGIST].strip()
                sh.worksheet(self.date_record).update_cell(row_num, 3, f'{client_record}')
                if (self.lst_records and search_criteria == '') or (self.lst_records and client_record == ''):
                    record = [self.date_record, self.time_record, self.location, self.psychologist]
                    if search_criteria == '':
                        self.lst_records.append(record)
                    else:
                        self.lst_records.remove(record)
                return True
        return False

    @retry(wait_exponential_multiplier=3000, wait_exponential_max=3000)
    def get_record(self, client_record: str, count_days=7) -> list:
        """
        Находит все записи клиента на ближайшие count_days дней

        :param client_record: Строка клиента
        :param count_days: Количество дней
        :return: Список [Дата, Время, Локация, Психолог]
        """
        if self.lst_records:
            return self.lst_records

        @retry(wait_exponential_multiplier=3000, wait_exponential_max=3000)
        def check_record(sheet_obj) -> None:
            if sheet_obj.title in IGNOR_WORKSHEETS:
                return None
            try:
                date_sheet = datetime.strptime(sheet_obj.title, '%d.%m.%Y')
            except Exception as ex:
                print(ex, sheet_obj.title, '- Добавьте лист в IGNOR_WORKSHEETS')
                return None
            date_today = datetime.now(tz=tz)
            if date_today.date() == date_sheet.date():
                with lock:
                    all_val = sheet_obj.get_all_records()
                for dct in all_val:
                    if dct['Клиент'] == client_record:
                        try:
                            record_time = datetime.strptime(dct['Время'], '%H:%M').time()
                            if date_today.time() < record_time:
                                print("Запись найдена:", [sheet_obj.title.strip(), dct['Время'].strip(),
                                                         self.location, dct[NAME_COL_PSYCHOLOGIST].strip()])
                                lst_records.append([sheet_obj.title.strip(), dct['Время'].strip(),
                                                   self.location, dct[NAME_COL_PSYCHOLOGIST].strip()])
                        except Exception as e:
                            print(f"Ошибка при разборе времени: {e}, значение: {dct['Время']}")
            elif date_today.date() < date_sheet.date() <= (date_today + timedelta(days=count_days)).date():
                with lock:
                    all_val = sheet_obj.get_all_records()
                lst_records.extend([sheet_obj.title.strip(), dct['Время'].strip(),
                                   self.location, dct[NAME_COL_PSYCHOLOGIST].strip()]
                                  for dct in all_val if dct['Клиент'] == client_record)

        lst_records = []
        with ThreadPoolExecutor(2) as executor:
            executor.map(check_record, sh.worksheets())
        self.lst_records = lst_records
        return lst_records