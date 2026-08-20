# Исследование алгоритмов прямоугольного покрытия

Каталог фиксирует литературу и выводы для стадий B-D. PDF лежат в
[`papers/`](papers/), но не попадают в Git: глобальный `.gitignore` исключает `*.pdf`.
После клонирования репозитория их можно повторно скачать по прямым ссылкам ниже.

> **Уточнение после встречи 2026-08-20:** MVP разрешает overlap прямоугольников
> (`demand_bbox`), считает площадь покрытия по их объединению без двойного зачёта и не
> суммирует `As` слабых зон. Расширение `40d`, фаза и раздвижка выполняются общей
> постобработкой. Актуальная сводка
> локальных материалов, нормативного
> контекста, раскроя 11 700 мм и плана простых MVP-алгоритмов находится в
> [`docs/layout-research-2026-08-19.md`](../docs/layout-research-2026-08-19.md).
> Rectangle-cover литература ниже остаётся полезной для синтеза/выбора областей, но её
> конфликтные ограничения должны учитывать и bbox зон, и физические оси стержней.

## Короткий вывод

Геометрическая часть задачи близка не к упаковке прямоугольников (`rectangle packing`),
а к **взвешенному прямоугольному покрытию**, дополненному детализацией массивов стержней:

- есть сетка КЭ с локальной потребностью `required_as`;
- кандидат — прямоугольная `LayoutZone` с направлением, диаметром и шагом;
- прямоугольник может накрывать разные уровни потребности и фон, но должен обеспечивать
  максимум требований всех накрытых значимых КЭ;
- недоармирование запрещено, перерасход превращается в массу;
- выбранные `demand_bbox` могут пересекаться; физические конфликты после детализации
  определяются фазой, осями и продольными интервалами и выводятся отдельно;
- цель сочетает массу и число параметрических зон либо строит Парето-фронт по этим двум
  показателям; физические стержни и позиции спецификации измеряются отдельно.

Для конечного набора кандидатов естественная постановка выглядит как weighted set cover
или covering integer program. Общий запрет `x_r + x_s <= 1` для геометрически
пересекающихся зон не нужен. Ограничения/repair применяются только к физическим
конфликтам, которые постобработка не смогла разрешить; в текущем MVP они явно
диагностируются.

## Что реализовано в MVP и что делать следующим

### 0. Общий builder/validator массивов стержней — реализовано

Обязательный слой из `docs/optimization-notes.md` реализован: ширина `k × step`,
`bar_count = k + 1`, фаза, длина с политикой анкеровки/раскроя, покрытие КЭ, коллизии
осей и масса. Добавлены `row-run-greedy` и `strip-profile-dp`. Полный `CandidateSet` и
solver перенесены на пост-MVP этап.

Для точного решателя нельзя оставлять только inclusion-maximal rectangles: при стоимости,
зависящей от площади и массы, меньший прямоугольник может быть выгоднее. Максимальные
кандидаты полезны как быстрый поднабор для эвристики, а полный набор должен включать
trim-варианты либо все допустимые границы.

### 1. `milp-cover` или `cpsat-cover` — точный oracle на общем наборе кандидатов

Один бинарный признак на кандидата, ограничения покрытия для каждого значимого КЭ,
конфликтные ограничения только для неустранимых физических коллизий, лимит зон и
минимизация массы. MILP удобен
для LP-bound и Парето-кривой; CP-SAT удобен для дискретных логических правил. Для первой
версии оба должны получать один и тот же `CandidateSet` и проверяться одним валидатором.

CP-SAT способен моделировать координаты прямоугольников напрямую, однако тогда условие
«какие КЭ попали внутрь и какой уровень `As` нужен» порождает много reified-ограничений.
Дискретные кандидаты здесь не обязаны быть эвристикой: если перечислены все прямоугольники
по допустимым границам, модель остаётся точной для выбранной дискретизации.

Первую целевую функцию лучше задавать ε-constraint способом: для каждого `K` минимизировать
массу при `sum(x_r) <= K`. Так получим понятную Парето-кривую без подбора искусственного
коэффициента между килограммами и деталями.

### 2. `local-search` / LNS — улучшение любого допустимого решения

Операции: сдвиг границы, усиление или ослабление уровня, merge соседей, split зоны,
удаление и повторное покрытие локального окна. Каждое изменение проходит общий
валидатор. Подход полезен после появления точной функции стоимости и эталонного MILP на
малых примерах.

### 3. `guillotine-dp` — точный baseline внутри класса BSP

Вместо одного жадно выбранного разреза перебрать допустимые горизонтальные и вертикальные
разрезы и сохранить для каждого подпрямоугольника Парето-набор `(details, mass)`. Это не
даёт глобального оптимума среди всех покрытий, но даёт доказуемый оптимум среди
гильотинных разбиений и показывает цену жадного выбора в текущем `bsp`.

### 4. `maxrect-greedy` — быстрый поднабор кандидатов + weighted greedy

Строить не «пустые» MER, а максимальные допустимые прямоугольники по threshold-маскам,
добавлять полезные trim-варианты и оценивать кандидата через

`mass + lambda_details + penalty_overstrength`.

Жадный шаг выбирает лучший прирост покрытия на единицу стоимости. После каждого выбора
нужны удаление конфликтующих кандидатов, pruning избыточных зон и локальные merge/split
улучшения. Это масштабируемый baseline, но не точный генератор и не гарантия глобального
оптимума.

### 5. Column generation / branch-and-price — только после измерения масштаба

Если полный `CandidateSet` окажется слишком большим, master-задача выбирает зоны, а
pricing-задача ищет прямоугольник с лучшей приведённой стоимостью. Максимальная по сумме
подматрица — естественное ядро pricing. Это сильный, но существенно более сложный этап;
для MVP он преждевременен.

### 6. Генетический алгоритм — следующий эвристический baseline

Геном кодирует набор зон либо последовательность `split/merge/shift/change-level`.
Fitness использует массу после `40d`/раздвижки и число зон, а crossover/mutation всегда
завершаются repair и общим валидатором. Для сравнения нужны одинаковый вычислительный
бюджет, фиксированные seed и несколько повторов. Это соответствует рекомендации
научного руководителя исследовать эвристические методы рядом с детерминированными.

### 7. `multilevel-benders` — только при аддитивной семантике пересечений

Если позднее будет подтверждена точная формула, по которой `As` нескольких
пересекающихся деталей суммируется, карту
требований можно рассматривать как целочисленную матрицу и раскладывать её в сумму
прямоугольных слоёв. Для этого подходят MIP/Benders-подходы из планирования IMRT. Пока
сами пересечения разрешены, но их `As` не складывается и одна зона должна самостоятельно
удовлетворять локальный уровень; такой модуль реализовывать не следует.

## Что не стоит брать как основной оптимизатор

- `MER` в смысле maximal empty rectangles ищет пустоты, а нам нужны зоны поставки
  арматуры. Полезна родственная техника перечисления максимальных **допустимых**
  прямоугольников, но сама по себе она не выбирает решение.
- Классические `bin packing`, `strip packing`, nesting и cutting stock размещают заданные
  детали в листе. У нас обратная задача: детали ещё требуется синтезировать из карты
  потребности.
- Чистая минимальная partition по контуру запрещает лишнее покрытие и обычно минимизирует
  только число прямоугольников. Это полезный нижний baseline и источник геометрических
  техник, но не полная модель массы и уровней `As`.
- ML не должен генерировать зоны в обход детерминированного валидатора. Он может выбирать
  веса Парето-критериев или порядок локального поиска.

## Доступный инженерный эталон

Первый golden-case плиты нуля уже даёт 79 позиций спецификации, 1019 физических стержней
и 3177,64 кг по четырём направлениям. Это достаточно для калибровки масштаба массы и
проверки агрегатов, но ещё недостаточно для inverse optimization границ зон: геометрия
ручной раскладки находится в DWG и пока показывается только как растр PDF.

До DWG→DXF нельзя подбирать веса алгоритма по визуальному сходству с одной картинкой.
После извлечения геометрии плиты следует разделить на train/eval: не выбирать `lambda` и
не оценивать итоговое качество на одной и той же раскладке. Технический контракт эталона
описан в [`docs/golden-case.md`](../docs/golden-case.md).

Инвентарь всех материалов: 15 четырёхнаправленных входных наборов, сгруппированных в 11
инженерских PDF-выдач. Явно размеченных «Точек 3» нет. Поэтому первая модель предпочтений
должна быть малопараметрической и проверяться leave-one-project-out; направления одной
плиты, позиции спецификации и физические стержни нельзя считать независимыми примерами.

## Аннотированная литература

Приоритет показывает полезность именно для текущей постановки, а не научное качество
работы.

| Приоритет | Работа | Что взять в проект | Локальный файл |
|---|---|---|---|
| A | Heinrich-Litan, Lübbecke, *Rectangle Covers Revisited Computationally* (2006), [DOI](https://doi.org/10.1145/1187436.1216583), [PDF](https://or.rwth-aachen.de/files/research/publications/rcover-jea.pdf) | ILP для rectangle cover, LP-relaxation, maximal rectangles и greedy rounding | `papers/01-rectangle-covers-revisited.pdf` |
| A | Hanauer, Seybold, Unterweger, *Covering Rectilinear Polygons with Area-Weighted Rectangles* (2024), [DOI](https://doi.org/10.1137/1.9781611977929.12), [PDF](https://arxiv.org/pdf/2312.08540) | Самая близкая функция `creation cost + area cost`, base rectangles, ILP и практические эвристики | `papers/02-area-weighted-rectangle-cover.pdf` |
| A | Demiröz, Altınel, Akarun, *Rectangle Blanket Problem* (2019), [DOI](https://doi.org/10.1016/j.ejor.2019.02.004), [PDF](https://arxiv.org/pdf/1910.01193) | BIP, branch-and-price, pricing прямоугольника, simulated annealing; важен разрешённый выход за исходную маску | `papers/03-rectangle-blanket-problem.pdf` |
| A | Koch, Marenco, *The Maximum 2D Subarray Polytope* (2022), [DOI](https://doi.org/10.1016/j.dam.2021.09.031), [PDF](https://repositorio.utdt.edu/bitstreams/543c88e2-8229-4f4f-8153-507a4d4fe9c2/download) | Максимальная взвешенная подматрица как pricing-задача для column generation | `papers/04-maximum-2d-subarray-polytope.pdf` |
| B | Porschen, *On Rectangular Covering Problems* (2009), [DOI](https://doi.org/10.1142/S0218195909002988), [PDF](https://kups.ub.uni-koeln.de/54907/1/zaik2007-533.pdf) | Exact DP с ценой из площади, периметра и числа прямоугольников; минимальные размеры сторон | `papers/05-rectangular-covering-dp.pdf` |
| B | Najman, *Covering Rectilinear Polygons by Rectangles* (2014, магистерская работа), [PDF](https://or.rwth-aachen.de/files/research/theses/2014/Master_Thesis.pdf) | Компактный обзор rectangle cover, lower bounds и разбор Partition/Expand | `papers/06-rectilinear-cover-survey.pdf` |
| B | Liou, Tan, Lee, *Minimum Rectangular Partition Problem for Simple Rectilinear Polygons* (1990), [DOI](https://doi.org/10.1109/43.55205), [PDF](https://ir.lib.nycu.edu.tw/server/api/core/bitstreams/fa99356d-1701-4965-ae44-4199238328c8/content) | Точный минимум непересекающихся прямоугольников для простого ортогонального полигона | `papers/07-minimum-rectangular-partition.pdf` |
| B | Kim, Lee, Ahn, *Rectangular Partitions of a Rectilinear Polygon* (2021), [arXiv](https://arxiv.org/abs/2111.01970), [PDF](https://arxiv.org/pdf/2111.01970) | Thick partition как родственная постановка минимальной ширины и DP по разрезам | `papers/08-thick-rectangular-partitions.pdf` |
| B | Kolliopoulos, Young, *Approximation Algorithms for Covering/Packing Integer Programs* (2005), [DOI](https://doi.org/10.1016/j.jcss.2005.05.002), [PDF](https://arxiv.org/pdf/cs/0205030) | Общая теория covering IP с кратностями; пригодится, если требования будут суммироваться | `papers/09-covering-packing-integer-programs.pdf` |
| C | Chakrabarty, Grant, Könemann, *On Column-Restricted and Priority Covering Integer Programs* (2010), [PDF](https://www.cs.dartmouth.edu/~deepc/PUBS/CGK-full.pdf) | Модель «уровень поставки должен быть не ниже локального приоритета»; концептуально близко цветовым уровням `As` | `papers/10-priority-covering-integer-programs.pdf` |
| C | Basu Roy, *Covering Simple Orthogonal Polygons with Rectangles* (2025), [DOI](https://doi.org/10.4230/LIPIcs.APPROX/RANDOM.2025.2), [PDF](https://arxiv.org/pdf/2406.16209) | Современный обзор сложности, local search и пределы локальных методов для interior cover | `papers/11-local-search-orthogonal-cover.pdf` |
| B | Van Droogenbroeck, Piérard, *Object Descriptors Based on a List of Rectangles* (2011), [DOI](https://doi.org/10.1007/978-3-642-21569-8_14), [PDF](https://orbi.uliege.be/bitstream/2268/23634/1/Vandroogenbroeck2011Object.pdf) | Алгоритм и код перечисления inclusion-maximal прямоугольников бинарной маски; сервис для эвристического candidate source | `papers/12-maximal-rectangle-enumeration.pdf` |
| B | Gonzalez, Razzazi, Shing, Zheng, *On Optimal Guillotine Partitions Approximating Optimal d-Box Partitions* (1994), [DOI](https://doi.org/10.1016/0925-7721(94)90013-2), [PDF](https://sites.cs.ucsb.edu/~teo/papers/CGTA-Guillo.pdf) | Точный DP внутри guillotine-класса и граница относительно unrestricted partition | `papers/13-optimal-guillotine-partitions.pdf` |
| B | Desrosiers, Lübbecke, *Selected Topics in Column Generation* (2005), [DOI](https://doi.org/10.1287/opre.1050.0234), [PDF](https://or.rwth-aachen.de/files/research/publications/cgsurvey.pdf) | Restricted master, pricing, стабилизация и branch-and-price при слишком большом наборе зон | `papers/14-column-generation-survey.pdf` |
| B | Perron, Didier, Gay, *The CP-SAT-LP Solver* (2023), [DOI](https://doi.org/10.4230/LIPIcs.CP.2023.3), [PDF](https://drops.dagstuhl.de/storage/00lipics/lipics-vol280-cp2023/LIPIcs.CP.2023.3/LIPIcs.CP.2023.3.pdf) | Что именно умеет CP-SAT как решатель дискретной модели; не заменяет геометрическую постановку | `papers/15-cp-sat-lp-solver.pdf` |
| Условный B | Çevik, *Optimal Decomposition of IMRT Fluence Maps Using Combinatorial Benders Cuts* (2011), [репозиторий](https://digitalarchive.library.bogazici.edu.tr/bitstreams/c7167bc9-3a0a-4c23-b060-f4d574a07639/download) | Разложение многоуровневой целочисленной матрицы в сумму прямоугольников; актуально только при подтверждённом суммировании `As` пересечений | `papers/16-multilevel-rectangle-decomposition.pdf` |
| C | Applegate et al., *Compressing Rectilinear Pictures and Minimizing Access Control Lists* (2007), [PDF](https://authors.library.caltech.edu/records/r20jx-cdq31/files/p1066-applegate.pdf?download=1) | Последовательные цветные rectangle rules и strip-DP; полезная аналогия, но «перезапись цвета» не является физикой арматуры | `papers/17-colored-rectangle-rules.pdf` |

Отдельно стоит прочитать Koch, Marenco, *A Hybrid Heuristic for the Rectilinear Picture
Compression Problem* (2023), [DOI](https://doi.org/10.1007/s10288-022-00515-3). Авторы
сочетают максимальные прямоугольники, atomic rectangles и оптимальный выбор подмножества.
Открытого авторского PDF при проверке не найдено, поэтому файл намеренно не скачан.

## Предлагаемый исследовательский протокол

Для каждого нового алгоритма запускать один и тот же набор малых синтетических и реальных
задач и сохранять:

1. допустимость общего валидатора;
2. массу, число `LayoutZone` и суммарный `bar_count` раздельно;
3. перерасход `As` и площадь покрытия фоновых КЭ;
4. время, число кандидатов и solver gap/lower bound, если он есть;
5. SVG/HTML с одинаковой картой требований;
6. Парето-точки при фиксированных лимитах зон, а не только одно взвешенное число.

На маленьких сетках `guillotine-dp`, MILP/CP-SAT и полный перебор должны служить oracle для
регрессионных тестов эвристик.
