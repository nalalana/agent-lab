"""
/backend/common/immudbIO/client.py
immudb连接与基本读写
"""
import random
import time
from immudb import constants
from immudb.client import ImmudbClient
import uuid
from typing import List
from common.immudbIO.errors import UnconfirmedWriteError

RETRY_MAX_COUNT = 10
SLEEP_MIN = 0.5
SLEEP_MAX = 1.5

class toumingClient:
    def __init__(self, client: ImmudbClient):
        self.client = client
        self.transactionStart = "BEGIN TRANSACTION;"
        self.transactionEnd = "COMMIT;"
        self.currentTx = None

    def openSession(self, login, password, db):
        return self.client.openSession(login, password, db)

    def newTx(self):
        interface = self.currentTx.newTx()
        return interface

    def commit(self):
        return self.currentTx.commit()

    def rollback(self):
        return self.currentTx.rollback()

    def openManagedSession(self, login, password, db):
        return self.client.openManagedSession(login, password, db)

    def get(self, key: bytes, atRevision: int = None):
        return self.client.get(key, atRevision=atRevision)

    def verifiedGet(self, key: bytes, atRevision: int = None):
        return self.client.verifiedGet(key, atRevision=atRevision)

    def set(self, key: bytes, value: bytes):
        return self.client.set(key, value)

    def closeSession(self):
        self.client.closeSession()

    def serverHigherOrEqualsToVersion(self, version: str):
        health = self.client.health()
        return self.compare_version(health.version, version) > -1

    def serverVersionEqual(self, version: str):
        health = self.client.health()
        return self.compare_version(health.version, version) == 0

    def executeWithTransaction(self, concatenatedParams: dict, queries: List[str], separator="\n"):
        toExecute = [self.transactionStart]
        toExecute.extend(queries)
        toExecute.append(self.transactionEnd)
        multiLineQuery = separator.join(toExecute)
        resp = self.client.sqlExec(multiLineQuery, concatenatedParams)
        assert (len(resp.txs) > 0 and not resp.ongoingTx)
        return resp

    def _generateTableName(self):
        return ("T" + str(uuid.uuid4()).replace("-", "")).lower()

    def generateKeyName(self):
        return ("K" + str(uuid.uuid4()).replace("-", "")).lower()

    def createTestTable(self, tabname, *fields: List[str]):
        fieldsJoined = ",".join(fields)
        try:
            if (self.currentTx):
                resp = self.currentTx.sqlExec(
                    "CREATE TABLE IF NOT EXISTS  {table} ({fieldsJoined});".format(
                        table=tabname, fieldsJoined=fieldsJoined)
                )
                return True, '创建成功'
            resp = self.client.sqlExec(
                "CREATE TABLE IF NOT EXISTS  {table} ({fieldsJoined});".format(
                    table=tabname, fieldsJoined=fieldsJoined)
            )
        except Exception as e:
            return False, f"Failed to create table {tabname}: {e}"
        return True, '创建成功'

    def addColumns(self, tabname, *fields: List[str]):
        fieldsJoined = ",".join(fields)
        if (self.currentTx):
            resp = self.currentTx.sqlExec(
                "ALTER TABLE {table} ADD COLUMN {fieldsJoined};".format(
                    table=tabname, fieldsJoined=fieldsJoined)
            )
            return tabname
        resp = self.client.sqlExec(
            "ALTER TABLE {table} ADD COLUMN {fieldsJoined};".format(
                table=tabname, fieldsJoined=fieldsJoined)
        )
        resp = self.client.listTables()
        assert tabname in resp
        return tabname

    def dropColumns(self, tabname, *fields: List[str]):
        fieldsJoined = ",".join(fields)
        if (self.currentTx):
            resp = self.currentTx.sqlExec(
                "ALTER TABLE {table} DROP COLUMN {fieldsJoined};".format(
                    table=tabname, fieldsJoined=fieldsJoined)
            )
            return tabname
        resp = self.client.sqlExec(
            "ALTER TABLE {table} DROP COLUMN {fieldsJoined};".format(
                table=tabname, fieldsJoined=fieldsJoined)
        )
        resp = self.client.listTables()
        assert tabname in resp
        return tabname

    def prepareInsertQuery(self, table: str, fields: List[str], values: List):
        fieldsJoined = ",".join(fields)
        paramsJoined = ",".join(values)
        return "INSERT INTO {table} ({fieldsJoined}) VALUES ({paramsJoined});".format(table=table,
                                                                                       fieldsJoined=fieldsJoined,
                                                                                       paramsJoined=paramsJoined)

    def prepareUpsertQuery(self, table: str, fields: List[str], values: List):
        fieldsJoined = ",".join(fields)
        paramsJoined = ",".join(values)
        return "UPSERT INTO {table} ({fieldsJoined}) VALUES ({paramsJoined});".format(table=table,
                                                                                       fieldsJoined=fieldsJoined,
                                                                                       paramsJoined=paramsJoined)

    def prepareSelectQuery(self, fromWhat: str, whatToSelect: List[str], conditions: List[str]):
        fieldsJoined = ",".join(whatToSelect)
        conditionsJoined = ",".join(conditions)

        constructed = "SELECT {fieldsJoined} FROM {table}".format(
            table=fromWhat, fieldsJoined=fieldsJoined)
        if (len(conditions) > 0):
            constructed = constructed + " WHERE " + conditionsJoined
        constructed = constructed + ";"

        return constructed

    def insertToTable(self, table: str, fields: List[str], values: List, params: dict):
        preparedQuery = self.prepareInsertQuery(table, fields, values)
        if (self.currentTx):
            try:
                resp = self.currentTx.sqlExec(preparedQuery, params)
                assert (len(resp.txs) > 0 and not resp.ongoingTx)
                return True, '插入成功'
            except Exception as e:
                return False, f'插入失败: {str(e)}'

        for _ in range(RETRY_MAX_COUNT):
            try:
                resp = self.client.sqlExec(preparedQuery, params)
                assert (len(resp.txs) > 0 and not resp.ongoingTx)
                return True, '插入成功'
            except Exception as e:
                print(f"Error inserting to table {table}: {e}")
                time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
        return False, f"Failed to insert to table {table} after {RETRY_MAX_COUNT} retries"
    
    def upsertToTable(self, table: str, fields: List[str], values: List, params: dict):
        preparedQuery = self.prepareUpsertQuery(table, fields, values)
        # Without an operation-level idempotency contract, a lost acknowledgement
        # cannot be retried safely: another writer may have changed the same cell.
        try:
            resp = (self.currentTx or self.client).sqlExec(preparedQuery, params)
        except Exception as exc:
            raise UnconfirmedWriteError() from exc
        if not self.currentTx and (not getattr(resp, "txs", None) or getattr(resp, "ongoingTx", False)):
            raise UnconfirmedWriteError()
        return resp

    def simpleSelect(self, fromWhat: str, whatToSelect: List[str], params: dict, *conditions: List[str],
                     columnNameMode=constants.COLUMN_NAME_MODE_NONE):
        preparedQuery = self.prepareSelectQuery(
            fromWhat, whatToSelect, conditions)
        if (self.currentTx):
            return self.currentTx.sqlQuery(preparedQuery, params, columnNameMode=columnNameMode)
        for _ in range(RETRY_MAX_COUNT):
            try:
                result = self.client.sqlQuery(preparedQuery, params, columnNameMode=columnNameMode)
                return result
            except Exception as e:
                print(f"Error selecting from table {fromWhat}: {e}")
                time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
        return False, f"Failed to select from table {fromWhat} after {RETRY_MAX_COUNT} retries"
        return result

    def compare_version(self, a: str, b: str):
        aNumber = a.split("-")
        bNumber = b.split("-")
        (aMajor, aMinor, aRev) = aNumber[0].split(".")
        (bMajor, bMinor, bRev) = bNumber[0].split(".")
        if (aMajor, aMinor, aRev) < (bMajor, bMinor, bRev):
            return -1
        elif (aMajor, aMinor, aRev) > (bMajor, bMinor, bRev):
            return 1
        else:
            return 0

    def getTableCount(self, table: str):
        for _ in range(RETRY_MAX_COUNT):
            try:
                return self.client.sqlQuery(f"SELECT COUNT(*) FROM {table};")[0][0]
            except Exception as e:
                print(f"Error getting table count for table {table}: {e}")
                time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
        return False, f"Failed to get table count for table {table} after {RETRY_MAX_COUNT} retries"


