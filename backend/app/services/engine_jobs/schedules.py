"""Durable clock, trading calendar and dataset publication subscriptions."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from graphlib import CycleError, TopologicalSorter

from pydantic import ValidationError
from tivelo_contracts.base import canonical_bytes, fingerprint, utc_datetime
from tivelo_contracts.errors import TiveloError
from tivelo_data import DataEngine

from .models import JobPlan
from .schedule_calendar import FrozenTradingSchedule, TradingCalendar, freeze_trading_days
from .schedule_models import IntervalTrigger, PublicationTrigger, ScheduleRequest, TradingDayTrigger
from .storage import JobError, now


def publication_support():
    try:
        from . import schedule_publication
    except ModuleNotFoundError as exc:
        if exc.name != "tivelo_data.publications":
            raise
        raise JobError("RULES_UNAVAILABLE", "该数据引擎版本不支持发布订阅") from exc
    return schedule_publication


class ScheduleStore:
    def __init__(self, jobs):
        self.jobs = jobs
        self._data = None
        self.database = jobs.path("schedules.sqlite3")
        with self.connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise JobError("SCHEMA_UNSUPPORTED", "研究计划索引版本高于当前程序")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schedules (id TEXT PRIMARY KEY, "
                "request_key TEXT UNIQUE NOT NULL, request BLOB NOT NULL, state TEXT NOT NULL)"
            )
            # Earlier hosts can still run their interval schedules in this workspace.
            connection.execute(
                "CREATE TABLE IF NOT EXISTS trading_day_schedules (id TEXT PRIMARY KEY, "
                "request_key TEXT UNIQUE NOT NULL, request BLOB NOT NULL, state TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE VIEW IF NOT EXISTS schedule_catalog AS "
                "SELECT id,request_key,request,state,0 AS trading_day FROM schedules UNION ALL "
                "SELECT id,request_key,request,state,1 AS trading_day FROM trading_day_schedules"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS extended_schedules (id TEXT PRIMARY KEY, "
                "request_key TEXT UNIQUE NOT NULL, request BLOB NOT NULL, state TEXT NOT NULL, "
                "trigger_type INTEGER NOT NULL CHECK(trigger_type BETWEEN 0 AND 2))"
            )
            connection.execute(
                "CREATE VIEW IF NOT EXISTS all_schedules AS "
                "SELECT id,request_key,request,state,trading_day AS trigger_type FROM schedule_catalog "
                "UNION ALL SELECT id,request_key,request,state,trigger_type FROM extended_schedules"
            )
            for table in ("schedules", "trading_day_schedules"):
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_extended_keys BEFORE INSERT ON {table} "
                    "WHEN EXISTS(SELECT 1 FROM extended_schedules) BEGIN SELECT CASE "
                    "WHEN EXISTS(SELECT 1 FROM extended_schedules WHERE request_key=NEW.request_key) "
                    "THEN RAISE(ABORT,'schedule key reserved') "
                    "WHEN (SELECT count(*) FROM all_schedules)>=256 THEN RAISE(ABORT,'schedule capacity') END; END"
                )
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_extended_limit BEFORE UPDATE OF state ON {table} "
                    "WHEN json_extract(OLD.state,'$.enabled')=0 AND json_extract(NEW.state,'$.enabled')=1 "
                    "AND EXISTS(SELECT 1 FROM extended_schedules) "
                    "AND (SELECT count(*) FROM all_schedules WHERE json_extract(state,'$.enabled')=1)>=64 "
                    "BEGIN SELECT RAISE(ABORT,'active schedule capacity'); END"
                )
            # An older writer knows only the interval table. It must respect reservations
            # made by newer schedules even while their dispatch is suspended on that host.
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS interval_schedule_reservations BEFORE INSERT ON schedules "
                "WHEN EXISTS(SELECT 1 FROM trading_day_schedules) BEGIN "
                "SELECT CASE WHEN EXISTS(SELECT 1 FROM trading_day_schedules WHERE request_key=NEW.request_key) "
                "THEN RAISE(ABORT,'schedule key reserved') "
                "WHEN (SELECT count(*) FROM schedule_catalog)>=256 THEN RAISE(ABORT,'schedule capacity') END; END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS interval_schedule_activation BEFORE UPDATE OF state ON schedules "
                "WHEN json_extract(OLD.state,'$.enabled')=0 AND json_extract(NEW.state,'$.enabled')=1 "
                "AND EXISTS(SELECT 1 FROM trading_day_schedules) "
                "AND (SELECT count(*) FROM schedule_catalog WHERE json_extract(state,'$.enabled')=1)>=64 "
                "BEGIN SELECT RAISE(ABORT,'active schedule capacity'); END"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, "
                "schedule_id TEXT NOT NULL, plan BLOB, state TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS deliveries_schedule ON deliveries(schedule_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS deliveries_pending ON deliveries(schedule_id) "
                "WHERE json_extract(state,'$.status') IN ('pending','prepared')"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS deliveries_jobs ON deliveries(schedule_id,json_extract(state,'$.job_id'))"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(schedule_id,json_extract(state,'$.due_at') DESC,id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS delivery_sequence ON deliveries(schedule_id,json_extract(state,'$.publication.sequence') DESC) "
                "WHERE json_extract(state,'$.publication.sequence') IS NOT NULL"
            )
            connection.execute("PRAGMA user_version=1")
            connection.commit()

    @contextmanager
    def connection(self):
        self.jobs.path("schedules.sqlite3")
        connection = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid schedule list limit")

    @staticmethod
    def _save(connection, table, state):
        if table not in {"schedules", "deliveries"}:
            raise ValueError("unknown schedule table")
        identity = state["schedule_id" if table == "schedules" else "delivery_id"]
        state["updated_at"] = now()
        values = (json.dumps(state), identity)
        updated = connection.execute(f"UPDATE {table} SET state=? WHERE id=?", values).rowcount
        if table == "schedules" and updated == 0:
            updated = connection.execute(
                "UPDATE trading_day_schedules SET state=? WHERE id=?", values
            ).rowcount
            if updated == 0:
                updated = connection.execute(
                    "UPDATE extended_schedules SET state=? WHERE id=?", values
                ).rowcount
        if updated != 1:
            raise JobError("ARTIFACT_CORRUPT", "计划或派发状态与索引不一致")

    @staticmethod
    def _decode(identity, payload, trigger_type):
        if not isinstance(payload, bytes) or hashlib.sha256(payload).hexdigest() != identity:
            raise JobError("ARTIFACT_CORRUPT", "研究触发计划指纹不一致")
        try:
            if trigger_type == 2:
                frozen = publication_support().FrozenPublicationSchedule.model_validate_json(
                    payload
                )
                return frozen.request, frozen
            if trigger_type == 1:
                frozen = FrozenTradingSchedule.model_validate_json(payload)
                if isinstance(frozen.request.trigger, TradingDayTrigger):
                    return frozen.request, frozen.calendar
            else:
                request = ScheduleRequest.model_validate_json(payload)
                if isinstance(request.trigger, IntervalTrigger):
                    return request, request.trigger
        except ValidationError:
            pass
        raise JobError("ARTIFACT_CORRUPT", "保存的研究触发计划格式无效")

    def _existing(self, connection, request):
        row = connection.execute(
            "SELECT id,request,trigger_type,state FROM all_schedules WHERE request_key=?",
            (request.schedule_key,),
        ).fetchone()
        if row is None:
            return None
        saved, _ = self._decode(*row[:3])
        if saved != request:
            raise JobError("DATA_CONFLICT", "相同计划键已绑定其他配置，请创建新计划")
        return json.loads(row[3])

    def create(self, request):
        request = ScheduleRequest.model_validate_json(request.model_dump_json())
        payload = canonical_bytes(request)
        if len(payload) > 2 * 1024**2:
            raise JobError("RESOURCE_LIMIT", "研究触发计划超过大小限制")
        created = now()
        calendar, origin = None, None
        if not isinstance(request.trigger, IntervalTrigger):
            with self.connection() as connection:
                existing = self._existing(connection, request)
                if existing is not None:
                    return existing
        if isinstance(request.trigger, PublicationTrigger):
            support = publication_support()
            origin = self.publications(dataset_id=request.trigger.dataset_id, tail=True).cursor
            payload = canonical_bytes(
                support.FrozenPublicationSchedule(request=request, origin=origin)
            )
            first, table, trigger_type = None, "extended_schedules", 2
        elif isinstance(request.trigger, TradingDayTrigger):
            calendar = freeze_trading_days(request.trigger, as_of=utc_datetime(created))
            payload = canonical_bytes(FrozenTradingSchedule(request=request, calendar=calendar))
            if len(payload) > 2 * 1024**2:
                raise JobError("RESOURCE_LIMIT", "锁定交易日历后的计划超过大小限制")
            first = calendar.occurrences[0].due_at
            table, trigger_type = "trading_day_schedules", 1
        else:
            first = request.trigger.starts_at
            table, trigger_type = "schedules", 0
        if any(
            spec.versions
            for node in request.template.nodes
            if (spec := node.capture or node.prepare)
        ):
            table = "extended_schedules"
        if len(payload) > 2 * 1024**2:
            raise JobError("RESOURCE_LIMIT", "研究触发计划超过大小限制")
        identity = hashlib.sha256(payload).hexdigest()
        state = {
            "schedule_id": identity,
            "request": request.model_dump(mode="json"),
            "enabled": False,
            "created_at": created,
            "updated_at": created,
            "next_due_at": first.isoformat() if first is not None else None,
            "last_delivery_id": None,
            "last_error": None,
        }
        if calendar is not None:
            state["calendar"] = calendar.summary()
        if origin is not None:
            state["publication_cursor"] = origin.model_dump(mode="json")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._existing(connection, request)
            if existing is not None:
                return existing
            if connection.execute("SELECT count(*) FROM all_schedules").fetchone()[0] >= 256:
                raise JobError("RESOURCE_LIMIT", "研究触发计划数量已达上限")
            values = (identity, request.schedule_key, payload, json.dumps(state))
            if table == "extended_schedules":
                connection.execute(
                    "INSERT INTO extended_schedules VALUES (?,?,?,?,?)", (*values, trigger_type)
                )
            else:
                connection.execute(f"INSERT INTO {table} VALUES (?,?,?,?)", values)
            connection.commit()
        return state

    def get(self, identity):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT state FROM all_schedules WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            raise JobError("NOT_FOUND", "研究触发计划不存在")
        return json.loads(row[0])

    def _definition(self, identity):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT request,trigger_type FROM all_schedules WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            raise JobError("NOT_FOUND", "研究触发计划不存在")
        return self._decode(identity, *row)

    def request(self, identity):
        return self._definition(identity)[0]

    def calendar(self, identity):
        _, clock = self._definition(identity)
        if not isinstance(clock, TradingCalendar):
            raise JobError("NOT_FOUND", "该触发计划没有锁定的交易日历")
        return clock

    def delivery_request(self, delivery):
        request, clock = self._definition(delivery["schedule_id"])
        if isinstance(request.trigger, PublicationTrigger):
            receipt = publication_support().check_delivery(delivery, clock)
            return request.job_request(delivery["request_key"], None, receipt.reference)
        due = utc_datetime(delivery["due_at"])
        delivery_id = hashlib.sha256(
            f"{delivery['schedule_id']}:{due.isoformat()}".encode()
        ).hexdigest()
        if (
            delivery["delivery_id"] != delivery_id
            or delivery["request_key"] != "schedule." + delivery_id
        ):
            raise JobError("ARTIFACT_CORRUPT", "派发时点与原请求键不一致")
        day = None
        if isinstance(clock, TradingCalendar):
            occurrence = clock.occurrence(due, due)
            if (
                occurrence is None
                or delivery.get("trading_date") != occurrence.trading_date.isoformat()
            ):
                raise JobError("ARTIFACT_CORRUPT", "派发交易日与锁定日历不一致")
            day = occurrence.trading_date
        return request.job_request(delivery["request_key"], day)

    def list(self, *, limit=50):
        self._limit(limit)
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT state FROM all_schedules ORDER BY json_extract(state,'$.created_at') DESC,id LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def set_enabled(self, identity, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state,trigger_type FROM all_schedules WHERE id=?", (identity,)
            ).fetchone()
            if row is None:
                raise JobError("NOT_FOUND", "研究触发计划不存在")
            state = json.loads(row[0])
            publication = row[1] == 2
            if enabled and not publication and state["next_due_at"] is None:
                pending = connection.execute(
                    "SELECT 1 FROM deliveries WHERE schedule_id=? "
                    "AND json_extract(state,'$.status') IN ('pending','prepared') LIMIT 1",
                    (identity,),
                ).fetchone()
                if pending is None:
                    raise JobError("STATE_CONFLICT", "计划时点已全部处理，请创建新计划")
            if enabled and not state["enabled"]:
                count = connection.execute(
                    "SELECT count(*) FROM all_schedules WHERE json_extract(state,'$.enabled')=1"
                ).fetchone()[0]
                if count >= 64:
                    raise JobError("RESOURCE_LIMIT", "同时启用的研究触发计划已达上限")
                if publication:
                    self._check_publication_cycles(connection, identity)
                    if state["last_delivery_id"] and state["last_error"]:
                        failed = connection.execute(
                            "SELECT state FROM deliveries WHERE id=? AND schedule_id=? AND json_extract(state,'$.status')='failed'",
                            (state["last_delivery_id"], identity),
                        ).fetchone()
                        if failed:
                            delivery = json.loads(failed[0])
                            delivery.update(
                                status="prepared" if delivery["plan_id"] else "pending", error=None
                            )
                            self._save(connection, "deliveries", delivery)
                    state["last_error"] = None
            state["enabled"] = enabled
            self._save(connection, "schedules", state)
            connection.commit()
        return state

    def _check_publication_cycles(self, connection, identity):
        rows = connection.execute(
            "SELECT id,request FROM extended_schedules WHERE trigger_type=2 AND (id=? OR json_extract(state,'$.enabled')=1)",
            (identity,),
        )
        graph = TopologicalSorter()
        for schedule_id, payload in rows:
            request, _ = self._decode(schedule_id, payload, 2)
            watched = request.trigger.dataset_id
            for node in request.template.nodes:
                if spec := node.capture or node.prepare:
                    for source in spec.datasets:
                        target = source.request.dataset_id
                        if target != watched:
                            graph.add(target, watched)
        try:
            graph.prepare()
        except CycleError as exc:
            raise JobError(
                "DATA_CONFLICT", "启用后数据集会相互触发发布，请将环路中的其他输入锁定为已发布版本"
            ) from exc

    def publications(self, **options):
        if options.get("after") is not None and not self.jobs.path("data/index.sqlite3").is_file():
            raise JobError("SNAPSHOT_NOT_FOUND", "找不到原订阅的数据索引")
        root = self.jobs.path("data")
        if self._data is None:
            self._data = DataEngine(root)
        if not hasattr(self._data, "publications"):
            raise JobError("RULES_UNAVAILABLE", "该数据引擎版本不支持发布订阅")
        return self._data.publications(**options)

    def check_publication_source(self, delivery):
        schedule = self.get(delivery["schedule_id"])
        request, frozen = self._definition(delivery["schedule_id"])
        receipt = publication_support().check_delivery(delivery, frozen)
        cursor = frozen.cursor(schedule["publication_cursor"])
        if self.publications(after=cursor, limit=1).items != (receipt.publication,):
            raise JobError("DATA_CONFLICT", "发布事件与原数据流当前的下一条记录不一致")
        contents = self._data.store.dataset_version(receipt.publication.version_id).contents
        scope = contents.scope
        for node in request.template.nodes:
            if spec := node.capture or node.prepare:
                for source in spec.datasets:
                    selected = source.request
                    if selected.dataset_id != receipt.publication.dataset_id:
                        continue
                    if (
                        scope is None
                        or contents.record_type != selected.record_type
                        or contents.price_basis != selected.price_basis
                        or scope.market_id != selected.market_id
                        or scope.asset_class != selected.asset_class
                        or scope.extension_field != selected.extension_field
                        or (selected.timeframe and selected.timeframe not in scope.timeframes)
                        or not set(selected.instrument_ids).issubset(scope.instrument_ids)
                    ):
                        raise JobError(
                            "DATA_CONFLICT", "发布版本与模板选择的市场、标的或数据口径不一致"
                        )

    def _claim_publications(self):
        # Never hold the schedule write lock while reading an external data index.
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id,request,state FROM extended_schedules WHERE trigger_type=2 "
                "AND json_extract(state,'$.enabled')=1 ORDER BY id"
            ).fetchall()
        result = []
        for identity, payload, encoded in rows:
            original = json.loads(encoded)
            page, error = None, None
            try:
                if last := original["last_delivery_id"]:
                    delivery = self.delivery(last)
                    if delivery.get("schedule_id") != identity or delivery.get("delivery_id") != last:
                        raise JobError("ARTIFACT_CORRUPT", "订阅指向了其他派发记录")
                    if delivery["status"] in {"pending", "prepared"}:
                        result.append(delivery)
                        continue
                _, frozen = self._decode(identity, payload, 2)
                cursor = frozen.cursor(original.get("publication_cursor"))
                page = self.publications(after=cursor, limit=1)
                if not page.items and page.cursor == cursor:
                    continue
            except (JobError, TiveloError, ValueError, KeyError, OSError, sqlite3.Error) as exc:
                if isinstance(exc, sqlite3.Error) and getattr(exc, "sqlite_errorcode", 0) & 255 in (
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                ):
                    continue
                error = str(getattr(exc, "code", "ARTIFACT_CORRUPT"))
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT state FROM extended_schedules WHERE id=?", (identity,)
                ).fetchone()
                if current is None or current[0] != encoded:
                    continue
                if error is not None:
                    original.update(enabled=False, last_error={"code": error})
                elif not page.items:
                    original["publication_cursor"] = page.cursor.model_dump(mode="json")
                else:
                    event = page.items[0]
                    receipt = publication_support().PublicationReceipt(
                        schedule_id=identity, stream_id=cursor.stream_id, publication=event
                    )
                    delivery = self._new_delivery(
                        identity, fingerprint(receipt), event.recorded_at, 0
                    )
                    delivery["publication"] = event.model_dump(mode="json")
                    self._insert_delivery(connection, delivery)
                    original["last_delivery_id"] = delivery["delivery_id"]
                    result.append(delivery)
                self._save(connection, "schedules", original)
                connection.commit()
        return result

    @staticmethod
    def _new_delivery(identity, delivery_id, due, missed):
        created = now()
        return {
            "delivery_id": delivery_id,
            "schedule_id": identity,
            "due_at": due.isoformat(),
            "missed_count": missed,
            "status": "pending",
            "request_key": "schedule." + delivery_id,
            "plan_id": None,
            "job_id": None,
            "error": None,
            "created_at": created,
            "updated_at": created,
        }

    @staticmethod
    def _insert_delivery(connection, delivery):
        connection.execute(
            "INSERT INTO deliveries VALUES (?,?,NULL,?)",
            (delivery["delivery_id"], delivery["schedule_id"], json.dumps(delivery)),
        )

    def claim_due(self, at):
        """Reserve at most one dispatch per enabled schedule; resume its outbox first."""
        at = utc_datetime(at)
        result = []
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id,request,state,trigger_type FROM all_schedules AS c WHERE trigger_type<2 AND json_extract(state,'$.enabled')=1 "
                "AND (json_extract(state,'$.next_due_at')<=? OR EXISTS(SELECT 1 FROM deliveries AS d "
                "WHERE d.schedule_id=c.id AND json_extract(d.state,'$.status') IN ('pending','prepared'))) "
                "ORDER BY id LIMIT 64",
                (at.isoformat(),),
            ).fetchall()
            for identity, payload, encoded, trading_day in rows:
                state = json.loads(encoded)
                pending = connection.execute(
                    "SELECT state FROM deliveries WHERE schedule_id=? "
                    "AND json_extract(state,'$.status') IN ('pending','prepared') ORDER BY id LIMIT 1",
                    (identity,),
                ).fetchone()
                if pending is not None:
                    result.append(json.loads(pending[0]))
                    continue
                if state["next_due_at"] is None:
                    continue
                try:
                    _, clock = self._decode(identity, payload, trading_day)
                    occurrence = clock.occurrence(at, utc_datetime(state["next_due_at"]))
                except (ValueError, JobError):
                    state.update(enabled=False, last_error={"code": "ARTIFACT_CORRUPT"})
                    self._save(connection, "schedules", state)
                    continue
                if occurrence is None:
                    continue
                due = occurrence.due_at
                delivery_id = hashlib.sha256(f"{identity}:{due.isoformat()}".encode()).hexdigest()
                delivery = self._new_delivery(identity, delivery_id, due, occurrence.missed_count)
                if occurrence.trading_date is not None:
                    delivery["trading_date"] = occurrence.trading_date.isoformat()
                self._insert_delivery(connection, delivery)
                following = occurrence.next_due_at
                state["next_due_at"] = following.isoformat() if following is not None else None
                state["last_delivery_id"] = delivery_id
                self._save(connection, "schedules", state)
                result.append(delivery)
            connection.commit()
        return result + self._claim_publications()

    def deliveries(self, identity, *, limit=50):
        self._limit(limit)
        publication = self.get(identity)["request"]["trigger"].get("kind") == "data_publication"
        order = (
            "json_extract(state,'$.publication.sequence')"
            if publication
            else "json_extract(state,'$.due_at')"
        )
        scope = (
            "AND json_extract(state,'$.publication.sequence') IS NOT NULL" if publication else ""
        )
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT state FROM deliveries WHERE schedule_id=? {scope} ORDER BY {order} DESC,id LIMIT ?",
                (identity, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def delivery(self, identity):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT state FROM deliveries WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            raise JobError("NOT_FOUND", "研究派发记录不存在")
        return json.loads(row[0])

    def has_active_job(self, identity, active_ids):
        if not active_ids:
            return False
        placeholders = ",".join("?" for _ in active_ids)
        with self.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM deliveries WHERE schedule_id=? "
                    f"AND json_extract(state,'$.job_id') IN ({placeholders}) LIMIT 1",
                    (identity, *active_ids),
                ).fetchone()
                is not None
            )

    def plan(self, delivery):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT plan FROM deliveries WHERE id=?", (delivery["delivery_id"],)
            ).fetchone()
        if row is None:
            raise JobError("NOT_FOUND", "研究派发记录不存在")
        if row[0] is None:
            return None
        plan = JobPlan.model_validate_json(row[0])
        if (
            plan.plan_id != delivery["plan_id"]
            or plan.request.idempotency_key != delivery["request_key"]
        ):
            raise JobError("ARTIFACT_CORRUPT", "研究派发计划指纹不一致")
        return plan

    def lock_plan(self, delivery, plan):
        expected = self.delivery_request(delivery)
        if plan.request != expected:
            raise JobError("DATA_CONFLICT", "研究派发输入与触发计划不一致")
        payload = canonical_bytes(plan)
        if len(payload) > 4 * 1024**2:
            raise JobError("RESOURCE_LIMIT", "研究派发计划超过大小限制")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT plan,state FROM deliveries WHERE id=?", (delivery["delivery_id"],)
            ).fetchone()
            if row is None:
                raise JobError("NOT_FOUND", "研究派发记录不存在")
            state = json.loads(row[1])
            if state["status"] not in {"pending", "prepared"} or (
                row[0] is not None and bytes(row[0]) != payload
            ):
                raise JobError("STATE_CONFLICT", "研究派发已经锁定其他计划或结束")
            state["status"], state["plan_id"] = "prepared", plan.plan_id
            connection.execute(
                "UPDATE deliveries SET plan=? WHERE id=?", (payload, delivery["delivery_id"])
            )
            self._save(connection, "deliveries", state)
            connection.commit()
        return state

    def finish(self, delivery, status, *, job_id=None, code=None):
        if status not in {"submitted", "skipped_overlap", "failed"}:
            raise ValueError("invalid dispatch result")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM deliveries WHERE id=?", (delivery["delivery_id"],)
            ).fetchone()
            if row is None:
                raise JobError("NOT_FOUND", "研究派发记录不存在")
            state = json.loads(row[0])
            if state["status"] not in {"pending", "prepared"}:
                raise JobError("STATE_CONFLICT", "研究派发已经结束")
            if status == "submitted" and (job_id is None or job_id != state["plan_id"]):
                raise JobError("ARTIFACT_CORRUPT", "派发回执与原计划不一致")
            state.update(status=status, job_id=job_id, error={"code": code} if code else None)
            self._save(connection, "deliveries", state)
            row = connection.execute(
                "SELECT state,request,trigger_type FROM all_schedules WHERE id=?",
                (state["schedule_id"],),
            ).fetchone()
            schedule = json.loads(row[0])
            if status == "failed":
                schedule.update(enabled=False, last_error=state["error"])
            else:
                schedule["last_error"] = None
                if row[2] == 2:
                    if status != "submitted":
                        raise JobError("STATE_CONFLICT", "数据发布订阅必须按序派发")
                    _, frozen = self._decode(state["schedule_id"], row[1], 2)
                    receipt = publication_support().check_delivery(state, frozen)
                    cursor = frozen.cursor(schedule["publication_cursor"])
                    if (
                        receipt.publication.sequence <= cursor.sequence
                        or schedule["last_delivery_id"] != state["delivery_id"]
                    ):
                        raise JobError("DATA_CONFLICT", "派发确认与订阅的待处理记录不一致")
                    schedule["publication_cursor"] = receipt.checkpoint(cursor).model_dump(
                        mode="json"
                    )
                elif schedule["next_due_at"] is None:
                    schedule["enabled"] = False
            self._save(connection, "schedules", schedule)
            connection.commit()
        return state
