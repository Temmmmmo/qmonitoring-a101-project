# Проверка сериализации на IronPython 2.7.12 — 7 сентября 2026

## Реальный сбой

Версия кнопки 0.1.1, `Python 2.7.12 (IronPython)`. После выбора как связанного CAD
`407861`, так и импортированного `407797` пользователь получает `UnicodeEncodeError`
в `json.encoder.py_encode_basestring_ascii`, до открытия JSON на запись. Это не
доказательство успешного чтения всей арматуры: `collect_report` мог вернуть `partial`.
На момент этого сбоя реальный JSON ещё не был получен. Позднее отчёт 0.1.2 подтвердил
успешное чтение и запись; следующий этап описан в разделе 0.2.0 ниже.

Воспроизведение на официальном IronPython 2.7.12, Linux arm64 / .NET 6.0.36:

```python
json.dumps({"nested": [{"display_value": u"12345678 см²"}]},
           ensure_ascii=True, allow_nan=False, indent=2)
```

Получена та же ошибка: `'ascii' codec can't encode character '\u441' in position 9`.
Синтетическая строка подобрана для воспроизведения ветки кодировщика; нельзя утверждать,
что именно такое значение было в модели специалиста: это синтетический reproducer.

В IronPython `str` и `unicode` используют `System.String`. ASCII-ветка Python-реализации
кодировщика пытается декодировать строку с символами Latin-1 как UTF-8 байты. Комбинация
кириллицы и `²` воспроизводит сбой. Поэтому одного приведения всех строк к `unicode`
недостаточно. Источники: [модель типов IronPython](https://ironpython.net/documentation/dotnet/dotnet.html),
[JSON encoder версии 2.7.12](https://github.com/IronLanguages/ironpython2/blob/ipy-2.7.12/Src/StdLib/Lib/json/encoder.py).

## Исправление 0.1.2

`serialize_report_utf8()` использует `ensure_ascii=False` и затем явный `.encode('utf-8')`.
Никаких `default=str`, `errors=ignore/replace`, смены глобальной кодировки или изменения
модели. `write_report_json()` сериализует до открытия файла и сохраняет `O_EXCL`.
Кнопка вызывает именно эти функции, они же проверяются отдельным runtime smoke.

`scripts/verify_revit_probe_runtime.py` проходит **9 проверок на настоящем IronPython**:
round-trip вложенного отчёта с кириллицей / `см²` / `Ø` / спецсимволами / emoji и
нативной .NET-строкой; запись/чтение по кириллическому пути; отказ от перезаписи;
отказ от NaN, +Inf, −Inf, произвольного объекта и цикла без пустого файла;
отказ от расширения `.rvt`. Числа и булевы значения сохраняют типы.

В smoke используется `System.IO.Directory.Delete` только для удаления собственного
синтетического временного каталога: `shutil.rmtree` в этом IronPython / .NET Core
Linux окружении ошибается при обработке директорий. В поставляемой кнопке такого
удаления нет. Контейнер получает только read-only mounts к runtime, публичному коду
и тестовому скрипту; сеть отключена, временные записи находятся в `/tmp` контейнера.

## Воспроизведение

Официальный архив: [IronPython.2.7.12.zip](https://github.com/IronLanguages/ironpython2/releases/download/ipy-2.7.12/IronPython.2.7.12.zip).
SHA-256 загруженного архива: `fe01eb6037411036f7f600736f9557b2eb44874cf1979252a4541349846505d7`.
Распаковать в `artifacts/revit_probe/ironpython_2_7_12/runtime`; эти зависимости **не входят**
в ZIP специалисту и не коммитятся. Образ `mcr.microsoft.com/dotnet/runtime:6.0` использован
только как изолированный тестовый runtime, не как production-среда проекта.

В командах ниже `PROJECT_ROOT` — абсолютный путь checkout, не путь к RVT.

```bash
docker run --rm --network none --read-only --tmpfs /tmp \
  --mount type=bind,source=PROJECT_ROOT/artifacts/revit_probe/ironpython_2_7_12/runtime,target=/ipy,readonly \
  --mount type=bind,source=PROJECT_ROOT/integrations/pyrevit/QMonitoring.extension,target=/extension,readonly \
  --mount type=bind,source=PROJECT_ROOT/scripts/verify_revit_probe_runtime.py,target=/check.py,readonly \
  --env IRONPYTHONPATH=/ipy/Lib --env DOTNET_ROLL_FORWARD=Major \
  mcr.microsoft.com/dotnet/runtime:6.0 \
  dotnet /ipy/netcoreapp3.1/ipy.dll /check.py --lib-dir /extension/lib --require-ironpython
```

Ожидаемый вывод: `Legacy ASCII error reproduced: True`,
`PASS: 9 serialization/file checks; no Revit API exercised`.
Начиная с 0.2.0 также выводится `PASS: 6 trial checks ...; no Revit API exercised`.

Также проверяется без Docker на обычном Python (старый сбой там не ожидается):

```bash
python3 scripts/verify_revit_probe_runtime.py --lib-dir integrations/pyrevit/QMonitoring.extension/lib
python3 -m pytest -q tests/integrations/test_revit_probe.py
```

Локально **51 passed** (включая запуск этого smoke на CPython); дополнительно выполнен
описанный выше запуск на IronPython 2.7.12. `ruff`, `compileall`, `git diff --check`
и проверка ZIP 0.1.2 прошли. ZIP по-прежнему содержит только пять файлов поставки.
Полный алгоритмический набор в этой точечной правке не перезапускался.

Граница доказательства: пройдены сериализация и файловый код на том же **Python-движке**,
но не на той же ОС / версии .NET и не внутри pyRevit. Размещение арматуры, фактическая
создание геометрии и инженерная приёмка этими runtime-проверками не подтверждаются.

## Расширение 0.2.0 после первого реального JSON

В реальном отчёте 0.1.2 все девять физических стержней прочитаны без ошибок.
Чекер 0.2.0 отдельно сравнивает requested 100 мм на Area и фактические интервалы
96,875 мм по осям; исходный JSON больше не даёт ложного `differs` при пересчёте.

Тот же runtime smoke теперь компилирует все четыре library-модуля и оба entry point
расширения настоящим IronPython 2.7.12. Поставляемый `qm_trial_geometry` проходит
проверку прямоугольного solid, плана от верхней грани, совпадения осей, отказа при
смещённом конце и внутренней петле. Всего 6 новых проверок сверх 9 прежних.
Обход дерева через os.walk в этом Linux/.NET runtime, как и удаление каталогов,
может неверно классифицировать директории, поэтому harness компилирует явный список
шести файлов. Это особенность локального harness, не обход проверки в кнопке.

Контроль транзакций, ошибок создания/Regenerate/readback, отсутствия Commit,
обязательного отката, неподтверждённого RollBack/Pending, восстановления ID/эталона,
отмены UI и файловой записи после отката покрыт offline API doubles на CPython.
Всего интеграционных тестов 106; полный локальный набор 416 passed / 62 skipped
(нет optional SciPy). `ruff`, `compileall` и `git diff --check` проходят.

Это подтверждает совместимость Python-кода и проверяемые ветви управления, но не
создание Rebar внутри Revit. В 0.2.0 нет Commit и оставления стержней в модели;
`passed_rolled_back` относится только к временному набору, не к production-выдаче.
