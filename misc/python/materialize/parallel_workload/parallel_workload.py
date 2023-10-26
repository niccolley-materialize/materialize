# Copyright Materialize, Inc. and contributors. All rights reserved.
#
# Use of this software is governed by the Business Source License
# included in the LICENSE file at the root of this repository.
#
# As of the Change Date specified in that file, in accordance with
# the Business Source License, use of this software will be governed
# by the Apache License, Version 2.0.

import argparse
import datetime
import os
import random
import sys
import threading
import time
from collections import Counter, defaultdict

import pg8000

from materialize.mzcompose import DEFAULT_SYSTEM_PARAMETERS
from materialize.mzcompose.composition import Composition
from materialize.parallel_workload.action import (
    Action,
    BackupRestoreAction,
    CancelAction,
    KillAction,
    ddl_action_list,
    dml_nontrans_action_list,
    fetch_action_list,
    read_action_list,
    write_action_list,
)
from materialize.parallel_workload.database import (
    MAX_CLUSTER_REPLICAS,
    MAX_CLUSTERS,
    MAX_KAFKA_SINKS,
    MAX_KAFKA_SOURCES,
    MAX_POSTGRES_SOURCES,
    MAX_ROLES,
    MAX_SCHEMAS,
    MAX_TABLES,
    MAX_VIEWS,
    MAX_WEBHOOK_SOURCES,
    Database,
)
from materialize.parallel_workload.executor import Executor, initialize_logging
from materialize.parallel_workload.settings import Complexity, Scenario
from materialize.parallel_workload.worker import Worker

SEED_RANGE = 1_000_000
REPORT_TIME = 10


def run(
    host: str,
    ports: dict[str, int],
    seed: str,
    runtime: int,
    complexity: Complexity,
    scenario: Scenario,
    num_threads: int | None,
    naughty_identifiers: bool,
    num_databases: int,
    composition: Composition | None,
) -> None:
    num_threads = num_threads or os.cpu_count() or 10
    random.seed(seed)

    print(
        f"--- Running with: --seed={seed} --threads={num_threads} --runtime={runtime} --complexity={complexity.value} --scenario={scenario.value} {'--naughty-identifiers ' if naughty_identifiers else ''}--databases={num_databases} (--host={host})"
    )
    initialize_logging()

    end_time = (
        datetime.datetime.now() + datetime.timedelta(seconds=runtime)
    ).timestamp()

    rng = random.Random(random.randrange(SEED_RANGE))
    databases = [
        Database(i, rng, seed, host, ports, complexity, scenario, naughty_identifiers)
        for i in range(num_databases)
    ]

    system_conn = pg8000.connect(
        host=host, port=ports["mz_system"], user="mz_system", database="materialize"
    )
    system_conn.autocommit = True
    with system_conn.cursor() as system_cur:
        system_exe = Executor(rng, system_cur, databases[0])
        system_exe.execute("ALTER SYSTEM SET enable_webhook_sources TO true")
        system_exe.execute(
            f"ALTER SYSTEM SET max_schemas_per_database = {MAX_SCHEMAS * 2}"
        )
        # The presence of ALTER TABLE RENAME can cause the total number of tables to exceed MAX_TABLES
        system_exe.execute(
            f"ALTER SYSTEM SET max_tables = {len(databases) * MAX_TABLES * 2}"
        )
        system_exe.execute(
            f"ALTER SYSTEM SET max_materialized_views = {len(databases) * MAX_VIEWS * 2}"
        )
        system_exe.execute(
            f"ALTER SYSTEM SET max_sources = {len(databases) * (MAX_WEBHOOK_SOURCES + MAX_KAFKA_SOURCES + MAX_POSTGRES_SOURCES) * 2}"
        )
        system_exe.execute(
            f"ALTER SYSTEM SET max_sinks = {len(databases) * MAX_KAFKA_SINKS * 2}"
        )
        system_exe.execute(
            f"ALTER SYSTEM SET max_roles = {len(databases) * MAX_ROLES * 2}"
        )
        system_exe.execute(
            f"ALTER SYSTEM SET max_clusters = {len(databases) * MAX_CLUSTERS * 2}"
        )
        system_exe.execute(
            f"ALTER SYSTEM SET max_replicas_per_cluster = {MAX_CLUSTER_REPLICAS * 2}"
        )
        # Most queries should not fail because of privileges
        system_exe.execute(
            "ALTER DEFAULT PRIVILEGES FOR ALL ROLES GRANT ALL PRIVILEGES ON TABLES TO PUBLIC"
        )
        system_exe.execute(
            "ALTER DEFAULT PRIVILEGES FOR ALL ROLES GRANT ALL PRIVILEGES ON TYPES TO PUBLIC"
        )
        system_exe.execute(
            "ALTER DEFAULT PRIVILEGES FOR ALL ROLES GRANT ALL PRIVILEGES ON SECRETS TO PUBLIC"
        )
        system_exe.execute(
            "ALTER DEFAULT PRIVILEGES FOR ALL ROLES GRANT ALL PRIVILEGES ON CONNECTIONS TO PUBLIC"
        )
        system_exe.execute(
            "ALTER DEFAULT PRIVILEGES FOR ALL ROLES GRANT ALL PRIVILEGES ON DATABASES TO PUBLIC"
        )
        system_exe.execute(
            "ALTER DEFAULT PRIVILEGES FOR ALL ROLES GRANT ALL PRIVILEGES ON SCHEMAS TO PUBLIC"
        )
        system_exe.execute(
            "ALTER DEFAULT PRIVILEGES FOR ALL ROLES GRANT ALL PRIVILEGES ON CLUSTERS TO PUBLIC"
        )
        for database in databases:
            database.create(system_exe)

            conn = pg8000.connect(
                host=host,
                port=ports["materialized"],
                user="materialize",
                database=database.name(),
            )
            conn.autocommit = True
            with conn.cursor() as cur:
                database.create_relations(Executor(rng, cur, database))
            conn.close()
    system_conn.close()

    workers = []
    threads = []
    worker_rng = random.Random(rng.randrange(SEED_RANGE))
    for i in range(num_threads):
        weights: list[float]
        if complexity == Complexity.DDL:
            weights = [60, 30, 30, 30, 10]
        elif complexity == Complexity.DML:
            weights = [60, 30, 30, 30, 0]
        elif complexity == Complexity.Read:
            weights = [60, 30, 0, 0, 0]
        else:
            raise ValueError(f"Unknown complexity {complexity}")
        action_list = worker_rng.choices(
            [
                read_action_list,
                fetch_action_list,
                write_action_list,
                dml_nontrans_action_list,
                ddl_action_list,
            ],
            weights,
        )[0]
        actions = [
            action_class(worker_rng) for action_class in action_list.action_classes
        ]
        worker = Worker(
            worker_rng,
            actions,
            action_list.weights,
            end_time,
            action_list.autocommit,
            system=False,
        )
        thread_name = f"worker_{i}"
        print(
            f"{thread_name}: {', '.join(action_class.__name__ for action_class in action_list.action_classes)}"
        )
        workers.append(worker)

        thread = threading.Thread(
            name=thread_name,
            target=worker.run,
            args=(host, ports["materialized"], "materialize", databases),
        )
        thread.start()
        threads.append(thread)

    if scenario == Scenario.Cancel:
        worker = Worker(
            worker_rng,
            [CancelAction(worker_rng, workers)],
            [1],
            end_time,
            autocommit=False,
            system=True,
        )
        workers.append(worker)
        thread = threading.Thread(
            name="cancel",
            target=worker.run,
            args=(host, ports["mz_system"], "mz_system", databases),
        )
        thread.start()
        threads.append(thread)
    elif scenario == Scenario.Kill:
        assert composition, "Kill scenario only works in mzcompose"
        worker = Worker(
            worker_rng,
            [KillAction(worker_rng, composition)],
            [1],
            end_time,
            autocommit=False,
            system=False,
        )
        workers.append(worker)
        thread = threading.Thread(
            name="kill",
            target=worker.run,
            args=(host, ports["materialized"], "materialize", databases),
        )
        thread.start()
        threads.append(thread)
    elif scenario == Scenario.BackupRestore:
        assert composition, "Backup & Restore scenario only works in mzcompose"
        worker = Worker(
            worker_rng,
            [BackupRestoreAction(worker_rng, composition, databases)],
            [1],
            end_time,
            autocommit=False,
            system=False,
        )
        workers.append(worker)
        thread = threading.Thread(
            name="kill",
            target=worker.run,
            args=(host, ports["materialized"], "materialize", databases),
        )
        thread.start()
        threads.append(thread)
    elif scenario in (Scenario.Regression, Scenario.Rename):
        pass
    else:
        raise ValueError(f"Unknown scenario {scenario}")

    num_queries = 0
    try:
        while time.time() < end_time:
            for thread in threads:
                if not thread.is_alive():
                    for worker in workers:
                        worker.end_time = time.time()
                    raise Exception(f"Thread {thread.name} failed, exiting")
            time.sleep(REPORT_TIME)
            print(
                "QPS: "
                + " ".join(
                    f"{worker.num_queries / REPORT_TIME:05.1f}" for worker in workers
                )
            )
            for worker in workers:
                num_queries += worker.num_queries
                worker.num_queries = 0
    except KeyboardInterrupt:
        print("Keyboard interrupt, exiting")
        for worker in workers:
            worker.end_time = time.time()

    for thread in threads:
        thread.join()

    conn = pg8000.connect(host=host, port=ports["materialized"], user="materialize")
    conn.autocommit = True
    with conn.cursor() as cur:
        for database in databases:
            print(f"Dropping database {database}")
            database.drop(Executor(rng, cur, database))
    conn.close()

    ignored_errors: defaultdict[str, Counter[type[Action]]] = defaultdict(Counter)
    num_failures = 0
    for worker in workers:
        for action_class, counter in worker.ignored_errors.items():
            ignored_errors[action_class].update(counter)
    for counter in ignored_errors.values():
        for count in counter.values():
            num_failures += count

    failed = 100.0 * num_failures / num_queries if num_queries else 0
    print(f"Queries executed: {num_queries} ({failed:.0f}% failed)")
    print("Error statistics:")
    for error, counter in ignored_errors.items():
        text = ", ".join(
            f"{action_class.__name__}: {count}"
            for action_class, count in counter.items()
        )
        print(f"  {error}: {text}")


def parse_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", type=str, default=str(int(time.time())))
    parser.add_argument("--runtime", default=600, type=int, help="Runtime in seconds")
    parser.add_argument(
        "--complexity",
        default="ddl",
        type=str,
        choices=[elem.value for elem in Complexity],
    )
    parser.add_argument(
        "--scenario",
        default="regression",
        type=str,
        choices=[elem.value for elem in Scenario],
    )
    parser.add_argument(
        "--threads",
        type=int,
        help="Number of threads to run, by default number of SMT threads",
    )
    parser.add_argument(
        "--naughty-identifiers",
        action="store_true",
        help="Whether to use naughty strings as identifiers, makes the queries unreadable",
    )
    parser.add_argument(
        "--databases",
        default=2,
        type=int,
        help="Number of databases to create and run against, 2 by default",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="parallel-workload",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run a parallel workload againt Materialize",
    )

    parser.add_argument("--host", default="localhost", type=str)
    parser.add_argument("--port", default=6875, type=int)
    parser.add_argument("--system-port", default=6877, type=int)
    parser.add_argument("--http-port", default=6876, type=int)
    parse_common_args(parser)

    args = parser.parse_args()

    ports: dict[str, int] = {
        "materialized": 6875,
        "mz_system": 6877,
        "http": 6876,
        "kafka": 9092,
        "schema-registry": 8081,
    }

    system_conn = pg8000.connect(
        host=args.host,
        port=ports["mz_system"],
        user="mz_system",
        database="materialize",
    )
    system_conn.autocommit = True
    with system_conn.cursor() as cur:
        # TODO: Currently the same as mzcompose default settings, add
        # more settings and shuffle them
        for key, value in DEFAULT_SYSTEM_PARAMETERS.items():
            cur.execute(f"ALTER SYSTEM SET {key} = '{value}'")
    system_conn.close()

    run(
        args.host,
        ports,
        args.seed,
        args.runtime,
        Complexity(args.complexity),
        Scenario(args.scenario),
        args.threads,
        args.naughty_identifiers,
        args.databases,
        composition=None,  # only works in mzcompose
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
