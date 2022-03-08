"""
TBD
"""

from os import X_OK
from ibapi.scanner import NO_ROW_NUMBER_SPECIFIED
import time
from datetime import date
import datetime
import sqlite3
import math
from functools import cmp_to_key

from ibapi import wrapper
from ibapi import utils
from ibapi import contract
from ibapi.client import EClient
from ibapi.utils import iswrapper

# types
from ibapi.common import * # @UnusedWildImport
from ibapi.order_condition import * # @UnusedWildImport
from ibapi.contract import * # @UnusedWildImport
from ibapi.order import * # @UnusedWildImport
from ibapi.order_state import * # @UnusedWildImport
from ibapi.execution import Execution
from ibapi.execution import ExecutionFilter
from ibapi.commission_report import CommissionReport
from ibapi.ticktype import * # @UnusedWildImport
from ibapi.tag_value import TagValue

from ibapi.account_summary_tags import *

from TraderOrder import TraderOrder

def printWhenExecuting(fn):
    def fn2(self):
        print("   doing", fn.__name__)
        fn(self)
        print("   done w/", fn.__name__)
    return fn2

def printinstance(inst: Object):
    attrs = vars(inst)
    print(', '.join("%s: %s" % item for item in attrs.items()))

class Trader(wrapper.EWrapper, EClient):

    def __init__(self):
        wrapper.EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self.nKeybInt = 0
        self.started = False
        self.nextValidOrderId = None
        self.db = None
        self.account = None
        self.portfolioNAV = None
        self.portfolioLoaded = False
        self.ordersLoaded = False
        self.lastCashAdjust = None
        self.lastNakedPutsSale = None
        self.lastRollOptionTime = None
        self.nextTickerId = 1024

        self.useCache = False
        self.lastWheelProcess = 0
        self.lastWheelRequestTime = None
        self.wheelSymbolsToProcess = []
        self.wheelSymbolsProcessingSymbol = None
        self.wheelSymbolsProcessingStrikes = []
        self.wheelSymbolsProcessingExpiration = None
        self.wheelSymbolsExpirations = []
        self.wheelSymbolsProcessed = []
        self.optionContractsAvailable = False
    
    def setUseCache(self, useit: bool):
        self.useCache = useit
        self.optionContractsAvailable = useit

    def getNextTickerId(self):
        self.nextTickerId += 1
        return self.nextTickerId

    def nextOrderId(self):
        oid = self.nextValidOrderId
        self.nextValidOrderId += 1
        return oid

    def getDbConnection(self):
        if self.db == None:
            self.db = sqlite3.connect('../db/var/db/data.db', 70)
        return self.db

    @printWhenExecuting
    def clearAllApiReqId(self):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute('UPDATE contract SET api_req_id = NULL')
        c.close()
        self.db.commit()

    def clearPortfolioBalances(self, accountName: str):
        self.getDbConnection()
        # clear currencies cash balances
        c = self.db.cursor()
        c.execute(
            'UPDATE balance SET quantity = 0 WHERE portfolio_id = (SELECT id from portfolio WHERE account = ?)',
            (accountName, ))
        c.close()
        self.db.commit()

    def clearPortfolioPositions(self, accountName: str):
        self.getDbConnection()
        # clear currencies cash balances
        c = self.db.cursor()
        c.execute(
            'UPDATE position SET quantity = 0 WHERE portfolio_id = (SELECT id from portfolio WHERE account = ?)',
            (accountName, ))
        c.close()
        self.db.commit()

    def clearOpenOrders(self, accountName: str):
        self.getDbConnection()
        # clear currencies cash balances
        c = self.db.cursor()
        c.execute(
            'DELETE FROM open_order WHERE account_id = (SELECT id from portfolio WHERE account = ?)',
            (accountName, ))
        c.close()
        self.db.commit()

    def clearOptionsPrices(self):
        self.getDbConnection()
        # clear currencies cash balances
        c = self.db.cursor()
        c.execute(
            """
            UPDATE contract
              SET price = NULL, ask = NULL, bid = NULL, updated = NULL, ask_date = NULL, bid_date = NULL
              WHERE contract.secType = 'OPT'
            """)
        rowcount = c.rowcount
        c.close()
        self.db.commit()
        return rowcount

    def findPortfolio(self, account: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (account, )
        c.execute('SELECT id FROM portfolio WHERE account = ?', t)
        r = c.fetchone()
        portfolio_id = int(r[0])
        c.close()
        return portfolio_id

    @staticmethod
    def normalizeSymbol(symbol):
        return symbol.rstrip('d').replace(' ', '-').replace('.T', '')

    def countApiRequestsInProgress(self):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute('SELECT COUNT(*) FROM contract WHERE api_req_id NOTNULL',)
        r = c.fetchone()
        count = int(r[0])
        c.close()
        return count

    def clearRequestId(self, reqId: int):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute('UPDATE contract SET api_req_id = NULL WHERE api_req_id = ?', (reqId, ))
        rowcount = c.rowcount
        c.close()
        self.db.commit()
        if rowcount == 1:
            count = self.countApiRequestsInProgress()
        else:
            count = -1
        return count

    def clearRequestIdAndContinue(self, reqId: int):
        count = self.clearRequestId(reqId)
        # print('clearRequestIdAndContinue(', reqId, '):', count)
        if (count == 0) and self.wheelSymbolsExpirations:
            # continue data fetching if no more request running
            self.processNextOptionExpiration()
        return count

#
# Trading settings
#

    def getBenchmark(self, accountName: str):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            'SELECT contract.con_id, contract.currency, contract.secType, contract.symbol, contract.exchange '
            ' FROM contract, portfolio '
            ' WHERE portfolio.account = ?'
            '  AND contract.id = portfolio.benchmark_id', (accountName, ))
        r = c.fetchone()
        getBenchmark = Contract()
        getBenchmark.exchange = 'SMART'
        getBenchmark.primaryExchange = r[4]
        getBenchmark.conId = r[0]
        getBenchmark.currency = r[1]
        getBenchmark.secType = r[2]
        getBenchmark.symbol = r[3]
        c.close()
        # print('getBenchmark:', getBenchmark)
        return getBenchmark

    def getBenchmarkAmountInBase(self, account: str):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            """
            SELECT contract.price * SUM(position.quantity) / currency.rate
            FROM portfolio, currency, contract, position
            WHERE portfolio.account = ?
            AND contract.id = portfolio.benchmark_id
            AND position.portfolio_id = portfolio.id AND position.contract_id = portfolio.benchmark_id
            AND currency.currency = contract.currency
            """,
            (account, ))
        r = c.fetchone()
        if r:
            result = float(r[0])
        else:
            result = 0
        c.close()
        # print('getBenchmarkAmountInBase:', result)
        return result

    def getWheelStocksToProcess(self):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            """
            SELECT DISTINCT(contract.symbol), SUM(trading_parameters.nav_ratio) nav_ratio_sum, contract.id, contract.con_id, contract.currency, contract.exchange
             FROM trading_parameters, contract 
             WHERE contract.id = trading_parameters.stock_id
             GROUP BY contract.symbol
             ORDER BY nav_ratio_sum DESC, contract.symbol ASC
            """
            )
        # result = c.fetchall()
        result = [ item[2] for item in c.fetchall() ]
        c.close()
        # print('getWheelStocksToProcess:', result)
        return result

    # def getWheelSymbolsToProcess(self):
    #     result = [item[0] for item in self.getWheelStocksToProcess()]
    #     return result

    def getWheelSymbolNavRatio(self, accountName: str, stock: str):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            'SELECT trading_parameters.nav_ratio '
            ' FROM portfolio, trading_parameters, contract '
            ' WHERE portfolio.account = ?'
            '  AND trading_parameters.portfolio_id = portfolio.id'
            '  AND contract.id = trading_parameters.stock_id'
            '  AND contract.symbol = ?'
            , (accountName, stock, ))
        r = c.fetchone()
        if r:
            result = float(r[0])
        else:
            result = 0
        c.close()
        self.db.commit()
        # print('getWheelSymbolNavRatio:(', stock, ')', result)
        return result

    def getNakedPutRatio(self, accountName: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (accountName, )
        c.execute(
            'SELECT portfolio.put_ratio'
            ' FROM portfolio'
            ' WHERE portfolio.account = ?'
            , t)
        r = c.fetchone()
        if r:
            getNakedPutRatio =float(r[0])
        else:
            getNakedPutRatio = 0
        c.close()
        self.db.commit()
        # print('getNakedPutRatio:', getNakedPutRatio)
        return getNakedPutRatio

    def getNakedPutSleep(self, accountName: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (accountName, )
        c.execute(
            'SELECT portfolio.sell_naked_put_sleep'
            ' FROM portfolio'
            ' WHERE portfolio.account = ?'
            , t)
        r = c.fetchone()
        getNakedPutSleep = int(r[0]) * 60
        c.close()
        self.db.commit()
#        print('getNakedPutSleep:', getNakedPutSleep)
        return getNakedPutSleep

    def getFindSymbolsSleep(self, accountName: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (accountName, )
        c.execute(
            'SELECT portfolio.find_symbols_sleep'
            ' FROM portfolio'
            ' WHERE portfolio.account = ?'
            , t)
        r = c.fetchone()
        getFindSymbolsSleep = int(r[0]) * 60
        c.close()
        self.db.commit()
#        print('getFindSymbolsSleep:', getFindSymbolsSleep)
        return getFindSymbolsSleep

    def getAdjustCashSleep(self, accountName: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (accountName, )
        c.execute(
            'SELECT portfolio.adjust_cash_sleep'
            ' FROM portfolio'
            ' WHERE portfolio.account = ?'
            , t)
        r = c.fetchone()
        result = int(r[0]) * 60
        c.close()
        self.db.commit()
#        print('getAdjustCashSleep:', result)
        return result

    def getCashStrategy(self, accountName: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (accountName, )
        c.execute(
            'SELECT portfolio.cash_strategy'
            ' FROM portfolio'
            ' WHERE portfolio.account = ?'
            , t)
        r = c.fetchone()
        result = int(r[0])
        c.close()
        self.db.commit()
#        print('getCashStrategy:', result)
        return result

    def getRollOptionsSleep(self, accountName: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (accountName, )
        c.execute(
            'SELECT portfolio.roll_Options_Sleep'
            ' FROM portfolio'
            ' WHERE portfolio.account = ?'
            , t)
        r = c.fetchone()
        if r[0]:
            getRollOptionsSleep = int(r[0]) * 60
        else:
            getRollOptionsSleep = 0
        c.close()
        self.db.commit()
#        print('getRollOptionsSleep:', getRollOptionsSleep)
        return getRollOptionsSleep

    def getMinPremium(self, accountName: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (accountName, )
        c.execute(
            'SELECT portfolio.min_premium'
            ' FROM portfolio'
            ' WHERE portfolio.account = ?'
            , t)
        r = c.fetchone()
        if r[0]:
            getMinPremium = float(r[0])
        else:
            getMinPremium = 0
        c.close()
        self.db.commit()
#        print('getMinPremium:', getMinPremium)
        return getMinPremium

    def getNakedPutWinRatio(self, accountName: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (accountName, )
        c.execute(
            'SELECT portfolio.naked_Put_Win_Ratio'
            ' FROM portfolio'
            ' WHERE portfolio.account = ?'
            , t)
        r = c.fetchone()
        if r[0]:
            getNakedPutWinRatio = float(r[0])
        else:
            getNakedPutWinRatio = 0
        c.close()
        self.db.commit()
#        print('getNakedPutWinRatio:', getNakedPutWinRatio)
        return getNakedPutWinRatio

    def getShortCallWinRatio(self, accountName: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (accountName, )
        c.execute(
            'SELECT portfolio.naked_call_Win_Ratio'
            ' FROM portfolio'
            ' WHERE portfolio.account = ?'
            , t)
        r = c.fetchone()
        if r[0]:
            result = float(r[0])
        else:
            result = 0
        c.close()
        self.db.commit()
#        print('getShortCallWinRatio:', result)
        return result

    def getRollDaysBefore(self, accountName: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (accountName, )
        c.execute(
            'SELECT portfolio.Roll_Days_Before'
            ' FROM portfolio'
            ' WHERE portfolio.account = ?'
            , t)
        r = c.fetchone()
        if r[0]:
            getRollDaysBefore = float(r[0])
        else:
            getRollDaysBefore = 1
        c.close()
        self.db.commit()
#        print('getRollDaysBefore:', result)
        return getRollDaysBefore

    def getCrawlDaysNumber(self, accountName: str):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            """
            SELECT portfolio.Crawler_Days
             FROM portfolio
             WHERE portfolio.account = ?
            """,
            (accountName, ))
        r = c.fetchone()
        if r[0]:
            result = float(r[0])
        else:
            result = 45
        c.close()
        self.db.commit()
#        print('getCrawlDaysNumber:', result)
        return result
    #
    # Other
    #

    def createOrUpdatePosition(self, contract: Contract, position: float, averageCost: float, accountName: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (accountName, )
        c.execute('SELECT id, base_currency FROM portfolio WHERE account = ?', t)
        r = c.fetchone()
        pid = r[0]

        t = (contract.conId, )
        c.execute('SELECT id FROM contract WHERE contract.con_id = ?', t)
        r = c.fetchone()
        cid = r[0]

        if position == 0:
            t = (pid, cid)
            c.execute('DELETE FROM position WHERE portfolio_id = ? AND contract_id = ?', t)
        else:
            t = (averageCost * position, position, pid, cid)
            c.execute('UPDATE position SET cost = ?, quantity = ? WHERE portfolio_id = ? AND contract_id = ?', t)
            #print(c.rowcount)
            if (c.rowcount == 0):
                c.execute("INSERT INTO position(cost, quantity, portfolio_id, contract_id, open_date) VALUES (?, ?, ?, ?, datetime('now'))", t)
        c.close()
        self.db.commit()

    """
    Contracts related functions
    """

    def getContractAsk(self, contract: Contract):
        self.getDbConnection()
        c = self.db.cursor()

        t = (contract.conId, )
        c.execute(
            'SELECT contract.ask ' \
            ' FROM contract ' \
            ' WHERE contract.con_id = ?',
            t)
        r = c.fetchone()
        if r[0]:
            result = float(r[0])
        else:
            result = None
        c.close()
        print('getContractAsk:', result)
        return result

    def getContractPrice(self, contract: Contract):
        if contract.conId:
            self.getDbConnection()
            c = self.db.cursor()
            c.execute(
                'SELECT contract.price ' \
                ' FROM contract ' \
                ' WHERE contract.con_id = ?',
                (contract.conId, ))
            r = c.fetchone()
            if r:
                result = float(r[0])
            else:
                result = None
            c.close()
        else:
            result = self.getSymbolPrice(contract.symbol)
        # print('getContractPrice:', result)
        return result

    def getUnderlyingPrice(self, contract: Contract):
        self.getDbConnection()
        c = self.db.cursor()

        t = (contract.conId, )
        c.execute(
            'SELECT stock_contract.price'
            ' FROM contract stock_contract, contract, option'
            ' WHERE contract.con_id = ?'
            '  AND option.id = contract.id'
            '  AND stock_contract.id = option.stock_id'
            , t)
        r = c.fetchone()
        if r[0]:
            result = float(r[0])
        else:
            result = None
        c.close()
#        print('getUnderlyingPrice(', contract.symbol, '):', result)
        return result

    def findContractById(self, id: int):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            """
            SELECT contract.con_id, contract.currency, contract.secType, contract.symbol, contract.exchange
             FROM contract
             WHERE contract.id = ?
            """,
            (id, )
        )
        r = c.fetchone()
        if r:
            result = Contract()
            if r[0]:
                result.conId = r[0]
            result.currency = r[1]
            result.secType = r[2]
            result.symbol = r[3]
            result.exchange = 'SMART'
            result.primaryExchange = r[4]
        else:
            result = None
        c.close()
        # print('findContractById:', result)
        return result

    def findContractByConId(self, id: int):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            """
            SELECT contract.con_id, contract.currency, contract.secType, contract.symbol, contract.exchange
             FROM contract
             WHERE contract.con_id = ?
            """,
            (id, )
        )
        r = c.fetchone()
        result = Contract()
        if r[0]:
            result.conId = r[0]
        result.currency = r[1]
        result.secType = r[2]
        result.symbol = r[3]
        result.exchange = 'SMART'
        result.primaryExchange = r[4]
        c.close()
        # print('findContractById:', result)
        return result

    def findContractBySymbol(self, symbol: str):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            """
            SELECT contract.con_id, contract.currency, contract.secType, contract.symbol, contract.exchange
             FROM contract
             WHERE contract.symbol = ?
            """,
            (symbol, )
        )
        r = c.fetchone()
        result = Contract()
        result.conId = r[0]
        result.currency = r[1]
        result.secType = r[2]
        result.symbol = r[3]
        result.exchange = 'SMART'
        result.primaryExchange = r[4]
        c.close()
        # print('findContractBySymbol:', result)
        return result

    def getContractConId(self, symbol: str):
        return self.findContractBySymbol(symbol).conId

    def getSymbolCurrency(self, symbol: str):
        return self.findContractBySymbol(symbol).currency

    def findOrCreateStockContract(self, contract: Contract):
        self.getDbConnection()
        c = self.db.cursor()
        # first search for conId
        t = (contract.conId, )
        c.execute('SELECT id FROM contract WHERE con_id = ?', t)
        r = c.fetchone()
        if not r:
            # search for stock symbol
            t = ('STK', self.normalizeSymbol(contract.symbol), )
            c.execute('SELECT id FROM contract WHERE secType = ? AND symbol = ?', t)
            r = c.fetchone()
            if not r:
                # print('stock contract not found:', contract)
                t = ('STK', self.normalizeSymbol(contract.symbol), contract.primaryExchange, contract.currency, contract.conId)
                c.execute('INSERT INTO contract(secType, symbol, exchange, currency, con_id) VALUES (?, ?, ?, ?, ?)', t)
                id = c.lastrowid
                t = (id, )
                c.execute('INSERT INTO stock(id) VALUES (?)', t)
                c.close()
                self.db.commit()
            else:
                print(contract)
                id = r[0]
                t = (contract.conId, 'STK', self.normalizeSymbol(contract.symbol), )
                c.execute('UPDATE contract SET con_id = ? WHERE secType = ? AND symbol = ?', t)
                c.close()
                self.db.commit()
        else:
            id = r[0]
            c.close()
        return id

    def findOrCreateOptionContract(self, contract: Contract):
        self.getDbConnection()
        # look for stock or Underlying stock
        c = self.db.cursor()
        # first search for conId
        c.execute('SELECT id FROM contract WHERE con_id = ?', (contract.conId, ))
        r = c.fetchone()
        if not r:
            # search for stock symbol
            t = ('STK', self.normalizeSymbol(contract.symbol), )
            c.execute('SELECT id, name FROM contract WHERE secType = ? AND symbol = ?', t)
            r = c.fetchone()
            if not r:
                # need to create stock
                print('Underlying stock contract not found:', contract)
                t = ('STK', self.normalizeSymbol(contract.symbol), contract.currency, contract.primaryExchange, )
                c.execute('INSERT INTO contract(secType, symbol, currency, exchange) VALUES (?, ?, ?, ?)', t)
                stockid = c.lastrowid
                t = (stockid, )
                c.execute('INSERT INTO stock(id) VALUES (?)', t)
                name = contract.localSymbol
            else:
                stockid = r[0]
                name = r[1]
            # search for option
            t = (stockid, contract.right, contract.strike, datetime.datetime.strptime(contract.lastTradeDateOrContractMonth, '%Y%m%d').date())
            c.execute('SELECT id FROM option WHERE stock_id = ? AND call_or_put = ? AND strike = ? AND last_trade_date = ?', t)
            r = c.fetchone()
            if not r:
                if contract.currency == 'GBP':
                    strike = contract.strike / 100
                else:
                    strike = contract.strike
                t = (contract.secType, '{} {} {:.1f} {}'.format(self.normalizeSymbol(contract.symbol), datetime.datetime.strptime(contract.lastTradeDateOrContractMonth, '%Y%m%d').strftime('%d%b%y').upper(), contract.strike, contract.right), contract.currency, contract.conId, name, )
                c.execute('INSERT INTO contract(secType, symbol, currency, con_id, name) VALUES (?, ?, ?, ?, ?)', t)
                id = c.lastrowid
                t = (id, stockid, contract.right, strike, datetime.datetime.strptime(contract.lastTradeDateOrContractMonth, '%Y%m%d').date(), contract.multiplier)
                c.execute('INSERT INTO option(id, stock_id, call_or_put, strike, last_trade_date, multiplier) VALUES (?, ?, ?, ?, ?, ?)', t)
                c.close()
            else:
                id = r[0]
                c.execute('UPDATE contract SET con_id = ? WHERE id = ?', (contract.conId, id, ))
                c.close()
            self.db.commit()
        else:
            id = r[0]
            c.close()
        return id

    def findOrCreateContract(self, contract: Contract):
        if contract.secType == 'STK':
            id = self.findOrCreateStockContract(contract)
        elif contract.secType == 'OPT':
            id = self.findOrCreateOptionContract(contract)
        else:
            print('unknown contract.secType: ', contract)
            id = None
        return id

    """
    Symbols related functions
    """

    def getSymbolPrice(self, symbol: str):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            'SELECT contract.price ' \
            ' FROM contract ' \
            ' WHERE contract.symbol = ?',
            (symbol, ))
        r = c.fetchone()
        if r[0]:
            result = float(r[0])
        else:
            result = None
        c.close()
#        print('getSymbolPrice:', result)
        return result

    def getSymbolPriceInBase(self, account: str, symbol: str):
        self.getDbConnection()
        c = self.db.cursor()
        # get base currency
        t = (account, )
        c.execute('SELECT portfolio.base_currency FROM portfolio WHERE portfolio.account = ?', t)
        r = c.fetchone()
        base_currency = r[0]
        t = (symbol, base_currency, )
        c.execute(
            'SELECT (contract.price / currency.rate) ' \
            'FROM contract, currency ' \
            'WHERE contract.symbol = ?' \
            ' AND contract.currency = currency.currency ' \
            ' AND currency.base = ?',
            t)
        r = c.fetchone()
        getSymbolPriceInBase = float(r[0])
        c.close()
        # print('getSymbolPriceInBase:', benchmarkPrice)
        return getSymbolPriceInBase
    
    def getContractBuyableQuantity(self, account: str, symbol: str):
        print('getContractBuyableQuantity.', 'account:', account, 'symbol', symbol)
        self.getDbConnection()
        c = self.db.cursor()
        t = (account, symbol, )
        c.execute(
            'SELECT (balance.quantity / contract.price) ' \
            'FROM portfolio, balance, contract ' \
            'WHERE portfolio.account = ? AND contract.symbol = ? ' \
            ' AND balance.portfolio_id = portfolio.id AND balance.currency = contract.currency ',
            t)
        r = c.fetchone()
        getContractBuyableQuantity = float(r[0])
        print('getContractBuyableQuantity:', getContractBuyableQuantity)
        c.close()
        return getContractBuyableQuantity

    """
    Get Cash positions information
    """

    def getTotalCashAmount(self, account: str):
        self.getDbConnection()
        c = self.db.cursor()
        # how much cash do we have?
        t = (account, )
        c.execute(
            'SELECT SUM(balance.quantity / currency.rate) ' \
            'FROM portfolio, balance, currency ' \
            'WHERE balance.portfolio_id = portfolio.id AND portfolio.account = ?' \
            ' AND balance.currency = currency.currency ' \
            ' AND currency.base = portfolio.base_currency',
            t)
        r = c.fetchone()
        result = float(r[0])
        # print('total cash:', result)
        c.close()
        return result

    def getCurrencyBalance(self, account: str, currency: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (account, currency, )
        c.execute(
            'SELECT balance.quantity ' \
            'FROM portfolio, balance ' \
            'WHERE portfolio.account = ? ' \
            ' AND balance.portfolio_id = portfolio.id ' \
            ' AND balance.currency = ?',
            t)
        r = c.fetchone()
        result = float(r[0])
        c.close()
        # print('getCurrencyBalance:', result)
        return result

    def getCurrencyBalanceInBase(self, account: str, currency: str):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            """
            SELECT balance.quantity * currency.rate
            FROM portfolio, balance, currency 
            WHERE portfolio.account = ? 
             AND balance.portfolio_id = portfolio.id 
             AND balance.currency = ?
             AND currency.base = portfolio.base_currency
            """,
            (account, currency, ))
        r = c.fetchone()
        result = float(r[0])
        c.close()
        # print('getCurrencyBalanceInBase:', result)
        return result

    def getBaseToCurrencyRate(self, account: str, currency: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (account, currency, )
        c.execute(
            'SELECT currency.rate ' \
            'FROM portfolio, currency ' \
            'WHERE portfolio.account = ? ' \
            ' AND currency.base = portfolio.base_currency ' \
            ' AND currency.currency = ?',
            t)
        r = c.fetchone()
        result = float(r[0])
        c.close()
        # print('getBaseToCurrencyRatio:', result)
        return result

    """
    Get positions information (stock)
    """

    def getPortfolioStocksValue(self, account: str, stock: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (account, 'STK', )
        sql = 'SELECT SUM(position.quantity * contract.price / currency.rate) ' \
            'FROM position, portfolio, contract, currency ' \
            'WHERE position.portfolio_id = portfolio.id AND portfolio.account = ? ' \
            ' AND position.contract_id = contract.id ' \
            ' AND contract.secType = ? ' \
            ' AND contract.currency = currency.currency ' \
            ' AND currency.base = portfolio.base_currency'
        if stock:
            t += (stock, )
            sql += ' AND contract.symbol = ?'
        c.execute(sql, t)
        r = c.fetchone()
        if r[0]:
            getPortfolioStocksValue = float(r[0])
        else:
            getPortfolioStocksValue = 0
        c.close()
        # print('getPortfolioStocksValue:', getPortfolioStocksValue)
        return getPortfolioStocksValue

    def getPortfolioStocksQuantity(self, account: str, stock: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (account, 'STK', )
        sql = 'SELECT SUM(position.quantity) ' \
            'FROM position, portfolio, contract ' \
            'WHERE position.portfolio_id = portfolio.id AND portfolio.account = ? ' \
            ' AND position.contract_id = contract.id ' \
            ' AND contract.secType = ? '
        if stock:
            t += (stock, )
            sql += ' AND contract.symbol = ?'
        c.execute(sql, t)
        r = c.fetchone()
        if r[0]:
            getPortfolioStocksQuantity = float(r[0])
        else:
            getPortfolioStocksQuantity = 0
        c.close()
        # print('getPortfolioStocksQuantity:', getPortfolioStocksQuantity)
        return getPortfolioStocksQuantity

    """
    Get positions information (options)
    """

    def getShortCallPositionQuantity(self, account: str, stock: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (account, stock, 'C', )
        c.execute(
            'SELECT SUM(position.quantity * option.multiplier), MIN(option.multiplier) ' \
            'FROM position, portfolio, contract stock_contract, option ' \
            'WHERE position.portfolio_id = portfolio.id AND portfolio.account = ? ' \
            ' AND position.quantity < 0 ' \
            ' AND option.id  = position.contract_id ' \
            ' AND stock_contract.id = option.stock_id AND stock_contract.symbol = ?' \
            ' AND option.call_or_put = ?',
            t)
        r = c.fetchone()
        if r[0]:
            getShortCallPositionQuantity = int(r[0])
            multiplier = int(r[1])
        else:
            getShortCallPositionQuantity = 0
            multiplier = 100
        c.close()
        #print('getShortCallPositionQuantity:', getShortCallPositionQuantity)
        return getShortCallPositionQuantity

    def getPortfolioOptionsValue(self, account: str, stock: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (account, 'OPT', )
        sql = 'SELECT SUM(position.quantity * contract.price * option.multiplier / currency.rate) ' \
            'FROM position, portfolio, contract, option, currency, contract stock_contract ' \
            'WHERE position.portfolio_id = portfolio.id AND portfolio.account = ? ' \
            ' AND position.contract_id = contract.id ' \
            ' AND contract.secType = ? AND position.contract_id = option.id ' \
            ' AND contract.currency = currency.currency ' \
            ' AND currency.base = portfolio.base_currency' \
            ' AND stock_contract.id = option.stock_id'
        if stock != None:
            t += (stock, )
            sql += ' AND stock_contract.symbol = ?'
        c.execute(sql, t)
        r = c.fetchone()
        if r:
            getPortfolioOptionsValue = float(r[0])
        else:
            getPortfolioOptionsValue = 0
        c.close()
        # print('getPortfolioOptionsValue(', account, stock, ') =>', getPortfolioOptionsValue)
        return getPortfolioOptionsValue

    # returned value is <= 0 in base currency
    def getNakedPutAmount(self, account: str, stock: str):
        self.getDbConnection()
        c = self.db.cursor()
        # how much do we need to cover ALL short puts?
        t = (account, 'OPT', 'P', stock, )
        c.execute(
            'SELECT SUM(position.quantity * option.strike * option.multiplier / currency.rate) ' \
            'FROM position, portfolio, contract, option, currency, contract stock ' \
            'WHERE position.portfolio_id = portfolio.id AND portfolio.account = ? ' \
            ' AND position.quantity < 0 AND position.contract_id = contract.id ' \
            ' AND contract.secType = ? AND position.contract_id = option.id AND option.call_or_put = ? ' \
            ' AND contract.currency = currency.currency ' \
            ' AND currency.base = portfolio.base_currency' \
            ' AND option.stock_id = stock.id AND stock.symbol = ?',
            t)
        r = c.fetchone()
        if r[0] != None:
            result = float(r[0])
        else:
            result = 0
        c.close()
        # print('getNakedPutAmount:', result)
        return result

    def getTotalNakedPutAmount(self, account: str):
        self.getDbConnection()
        c = self.db.cursor()
        # how much do we need to cover ALL short puts?
        t = (account, 'OPT', 'P', )
        c.execute(
            'SELECT SUM(position.quantity * option.strike * option.multiplier / currency.rate) ' \
            'FROM position, portfolio, contract, option, currency ' \
            'WHERE position.portfolio_id = portfolio.id AND portfolio.account = ? ' \
            ' AND position.quantity < 0 AND position.contract_id = contract.id ' \
            ' AND contract.secType = ? AND position.contract_id = option.id AND option.call_or_put = ? ' \
            ' AND contract.currency = currency.currency ' \
            ' AND currency.base = portfolio.base_currency',
            t)
        r = c.fetchone()
        naked_puts_engaged = float(r[0])
        c.close()
        # print('total naked put:', naked_puts_engaged)
        return naked_puts_engaged

    def getWeightedShortedOptionsAmountInBase(self, account: str, put_or_call: str):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            """
            SELECT SUM(position.quantity * option.strike * option.multiplier * option.delta / currency.rate) 
            FROM position, portfolio, contract, option, currency, contract stock 
            WHERE position.portfolio_id = portfolio.id AND portfolio.account = ? 
              AND position.quantity < 0 
              AND position.contract_id = contract.id 
              AND contract.secType = 'OPT'
              AND position.contract_id = option.id 
              AND option.call_or_put = ? 
              AND option.stock_id = stock.id 
              AND contract.currency = currency.currency 
              AND currency.base = portfolio.base_currency
            """,
            (account, put_or_call, ))
        r = c.fetchone()
        if r[0]:
            result = -float(r[0])
        else:
            result = 0
        c.close()
        print('getWeightedShortedOptionsAmountInBase(', account, ',', put_or_call, '):', result)
        return result

    def getItmShortOptionsAmount(self, account: str, put_or_call: str):
        self.getDbConnection()
        c = self.db.cursor()
        sql = """
            SELECT SUM(position.quantity * option.strike * option.multiplier / currency.rate) 
            FROM position, portfolio, contract, option, currency, contract stock 
            WHERE position.portfolio_id = portfolio.id AND portfolio.account = ? 
             AND position.quantity < 0 
             AND position.contract_id = contract.id 
             AND contract.secType = 'OPT'
             AND position.contract_id = option.id 
             AND option.call_or_put = ? 
             AND option.stock_id = stock.id 
             AND contract.currency = currency.currency 
             AND currency.base = portfolio.base_currency
            """
        if put_or_call == 'C':
            sql += 'AND stock.price > option.strike'
        elif put_or_call == 'P':
            sql += 'AND stock.price < option.strike'
        c.execute(sql, (account, put_or_call, ))
        r = c.fetchone()
        c.close()
        if not r[0]:
            result = 0
        elif put_or_call == 'P':
            result = float(r[0])
        elif put_or_call == 'C':
            result = -float(r[0])
        else:
            result = 0
        print('getItmShortOptionsAmount(', account, ',', put_or_call, '):', result)
        return result

    """
    order book operations
    """

    def cancelStockOrderBook(self, account: str, symbol: str, action: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (account, symbol, 'STK', action, 'Submitted', 'PreSubmitted', )
        c.execute(
            'SELECT open_order.order_id '\
            'FROM open_order, portfolio, contract ' \
            'WHERE open_order.account_id = portfolio.id AND portfolio.account = ? ' \
            ' AND open_order.contract_id = contract.id AND contract.symbol = ? AND contract.secType = ? ' \
            ' AND open_order.action_type = ?' \
            ' AND open_order.status IN (?, ?)',
            t)
        for r in c:
            print('canceling order:', action, r[0])
            self.cancelOrder(int(r[0]))
        c.close()

    """
    Get order book information (stocks)
    """

    def getContractQuantityOnOrderBook(self, account: str, contract: Contract, action: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (account, contract.conId, action, 'Submitted', 'PreSubmitted', )
        c.execute(
            'SELECT SUM(remaining_qty) '\
            'FROM open_order, portfolio, contract ' \
            'WHERE open_order.account_id = portfolio.id AND portfolio.account = ? ' \
            ' AND open_order.contract_id = contract.id AND contract.con_id = ?' \
            ' AND open_order.action_type = ?' \
            ' AND open_order.status IN (?, ?)',
            t)
        r = c.fetchone()
        if r[0]:
            if action == 'BUY':
                getContractQuantityOnOrderBook = float(r[0])
            elif action == 'SELL':
                getContractQuantityOnOrderBook = -float(r[0])
        else:
            getContractQuantityOnOrderBook = 0
        c.close()
#        print('getContractQuantityOnOrderBook:', getContractQuantityOnOrderBook)
        return getContractQuantityOnOrderBook

    """
    Get order book information (stocks)
    """

    def getStockQuantityOnOrderBook(self, account: str, symbol: str, action: str):
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            """
            SELECT SUM(remaining_qty)
            FROM open_order, portfolio, contract 
            WHERE open_order.account_id = portfolio.id AND portfolio.account = ? 
             AND open_order.contract_id = contract.id AND contract.symbol = ? AND contract.secType = ? 
             AND open_order.action_type = ?
             AND open_order.status IN ('Submitted', 'PreSubmitted')
             """,
            (account, symbol, 'STK', action, ))
        r = c.fetchone()
        if r[0]:
            if action == 'BUY':
                result = float(r[0])
            elif action == 'SELL':
                result = -float(r[0])
        else:
            result = 0
        c.close()
        #print('getStockQuantityOnOrderBook:', result)
        return result

    """
    Get order book information (options)
    """

    def getOptionsQuantityOnOrderBook(self, account: str, stock: str, putOrCall: str, action: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (account, putOrCall, stock, action, 'Submitted', 'PreSubmitted', )
        c.execute(
            """
            SELECT SUM(open_order.remaining_qty * option.multiplier)
              FROM open_order, portfolio, option, contract
              WHERE open_order.account_id = portfolio.id AND portfolio.account = ?
                AND open_order.contract_id = option.id AND option.call_or_put = ? AND option.stock_id = contract.id AND contract.symbol = ?
                AND open_order.action_type = ?
                AND open_order.status IN (?, ?)
            """, t)
        r = c.fetchone()
        if r[0]:
            if action == 'BUY':
                result = float(r[0])
            elif action == 'SELL':
                result = -float(r[0])
        else:
            result = 0
        c.close()
        #print('getOptionsQuantityOnOrderBook:', result)
        return result

    def getOptionsAmountOnOrderBook(self, account: str, stock: str, putOrCall: str, action: str):
        self.getDbConnection()
        c = self.db.cursor()
        t = (account, putOrCall, action, 'Submitted', 'PreSubmitted')
        sql = """
            SELECT SUM(open_order.remaining_qty * option.multiplier * option.strike / currency.rate)
            FROM open_order, portfolio, option, contract, currency
            WHERE open_order.account_id = portfolio.id AND portfolio.account = ?
             AND open_order.contract_id = option.id AND option.call_or_put = ? AND option.stock_id = contract.id
             AND contract.currency = currency.currency
             AND currency.base = portfolio.base_currency
             AND open_order.action_type = ?
             AND open_order.status IN (?, ?)
            """
        if stock:
            t += (stock, )
            sql = sql + ' AND contract.symbol = ? '
        c.execute(sql, t)
        r = c.fetchone()
        if r[0]:
            if action == 'BUY':
                getOptionsAmountOnOrderBook = float(r[0])
            elif action == 'SELL':
                getOptionsAmountOnOrderBook = -float(r[0])
        else:
            getOptionsAmountOnOrderBook = 0
        c.close()
        # print('getOptionsAmountOnOrderBook(', account, stock, putOrCall, action, ') =>', getOptionsAmountOnOrderBook)
        return getOptionsAmountOnOrderBook

    """
    IB API wrappers
    """

    @iswrapper
    def error(self, reqId: TickerId, errorCode: int, errorString: str):
        # super().error(reqId, errorCode, errorString)
        if errorCode == 162:
            # Historical Market Data Service error message:HMDS query returned no data: PFSI@SMART Historical_Volatility
            super().error(reqId, errorCode, errorString)
            self.clearRequestId(reqId)
        elif errorCode == 200:
            # 'No security definition has been found for the request':
#            super().error(reqId, errorCode, errorString)
            if self.clearRequestId(reqId) == -1:
                # self.reqContractDetailsProcessingCount -= 1
                pass
        elif errorCode == 321:
            # Error validating request.-'bW' : cause - Snapshot requests limitation exceeded:100 per 1 second(s)
            super().error(reqId, errorCode, errorString)
            self.clearRequestId(reqId)
        elif errorCode == 10090:
            # Part of requested market data is not subscribed. Subscription-independent ticks are still active.Delayed market data is not available
#            super().error(reqId, errorCode, errorString)
            pass
        else:
            super().error(reqId, errorCode, errorString)
            count = self.countApiRequestsInProgress()
            # print(count, 'pending requests')

    @iswrapper
    # reqMktData callback
    def tickPrice(self, reqId: TickerId, tickType: TickType, price: float, attrib: TickAttrib):
#        print("TickPrice. TickerId:", reqId, "tickType:", tickType, "Price:", price, "CanAutoExecute:", attrib.canAutoExecute, "PastLimit:", attrib.pastLimit, "PreOpen:", attrib.preOpen)
        super().tickPrice(reqId, tickType, price, attrib)
        if price >= 0:
            self.getDbConnection()
            c = self.db.cursor()
            t = (round(price, 2), reqId, )
            if (tickType == TickTypeEnum.LAST) or (tickType == TickTypeEnum.DELAYED_LAST):   # 4
                c.execute('UPDATE contract SET price = ?, updated = datetime(\'now\') WHERE api_req_id = ?', t)
                if c.rowcount != 1:
                    print("TickPrice. TickerId:", reqId, "tickType:", tickType, "Price:", price, "CanAutoExecute:", attrib.canAutoExecute, "PastLimit:", attrib.pastLimit, "PreOpen:", attrib.preOpen)
                    print('failed to store price:', c.rowcount, 'row(s) affected')
            elif tickType == TickTypeEnum.BID:  # 1
                c.execute('UPDATE contract SET bid = ?, bid_date = datetime(\'now\') WHERE api_req_id = ?', t)
            elif tickType == TickTypeEnum.ASK:  # 2
                c.execute('UPDATE contract SET ask = ?, ask_date = datetime(\'now\') WHERE api_req_id = ?', t)
            elif (tickType == TickTypeEnum.CLOSE) or (tickType == TickTypeEnum.DELAYED_CLOSE):    # 9
                c.execute('UPDATE contract SET previous_close_price = ? WHERE api_req_id = ?', t)
            elif ((tickType == TickTypeEnum.HIGH) \
                or (tickType == TickTypeEnum.DELAYED_HIGH) \
                or (tickType == TickTypeEnum.LOW) \
                or (tickType == TickTypeEnum.DELAYED_LOW) \
                or (tickType == TickTypeEnum.OPEN) \
                or (tickType == TickTypeEnum.DELAYED_OPEN)):   # 6 & 7
                pass
            else:
                print('tickPrice. unexpected type:', tickType, 'for reqId:', reqId)
            c.close()
            self.db.commit()
    # ! [tickprice]

    @iswrapper
    # reqMktData callback
    def tickOptionComputation(self, reqId: TickerId, tickType: TickType, tickAttrib: int,
                              impliedVol: float, delta: float, optPrice: float, pvDividend: float,
                              gamma: float, vega: float, theta: float, undPrice: float):
#        print("TickOptionComputation. TickerId:", reqId, "TickType:", tickType, "TickAttrib:", tickAttrib, "ImpliedVolatility:", impliedVol, "Delta:", delta, "OptionPrice:", optPrice, "pvDividend:", pvDividend, "Gamma: ", gamma, "Vega:", vega, "Theta:", theta, "UnderlyingPrice:", undPrice)
        super().tickOptionComputation(reqId, tickType, tickAttrib, impliedVol, delta,
                                      optPrice, pvDividend, gamma, vega, theta, undPrice)
        self.getDbConnection()
        c = self.db.cursor()
        if tickType == TickTypeEnum.MODEL_OPTION: # 13
# what is optPrice in this case?
#            c.execute('UPDATE contract SET price = ?, updated = datetime(\'now\') WHERE id = (SELECT id from option WHERE api_req_id = ?)', t)
            if impliedVol:
                impliedVol = round(impliedVol, 3)
            if delta:
                delta = round(delta, 3)
            t = (impliedVol, delta, pvDividend, gamma, vega, theta, reqId, )
            c.execute(
                'UPDATE option '
                ' SET Implied_Volatility = ?, Delta = ?, pv_Dividend = ?, Gamma = ?, Vega = ?, Theta = ? '
                ' WHERE option.id = (SELECT contract.id FROM contract where contract.api_req_id = ?)', 
                t)
        else:
            if optPrice:
                optPrice = round(optPrice, 3)
            t = (optPrice, reqId, )
            if tickType == TickTypeEnum.BID_OPTION_COMPUTATION:   # 10
                c.execute('UPDATE contract SET bid = ?, bid_date = NULL WHERE api_req_id = ?', t)
            elif tickType == TickTypeEnum.ASK_OPTION_COMPUTATION:   # 11
                c.execute('UPDATE contract SET ask = ?, ask_date = NULL WHERE api_req_id = ?', t)
            elif tickType == TickTypeEnum.LAST_OPTION_COMPUTATION:  # 12
                c.execute('UPDATE contract SET price = ?, updated = NULL WHERE api_req_id = ?', t)
                if c.rowcount != 1:
                    print("TickOptionComputation. TickerId:", reqId, "TickType:", tickType, "TickAttrib:", tickAttrib, "ImpliedVolatility:", impliedVol, "Delta:", delta, "OptionPrice:", optPrice, "pvDividend:", pvDividend, "Gamma: ", gamma, "Vega:", vega, "Theta:", theta, "UnderlyingPrice:", undPrice)
                    print('failed to store price:', c.rowcount, 'row(s) affected')
            else:
                print('TickOptionComputation. unexpected type:', tickType, 'for reqId:', reqId)
        c.close()
        self.db.commit()
    # ! [tickoptioncomputation]

    @iswrapper
    # reqMktData callback
    def tickSnapshotEnd(self, reqId: int):
        super().tickSnapshotEnd(reqId)
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            'SELECT contract.con_id, contract.currency, contract.secType, contract.symbol FROM contract WHERE contract.api_req_id = ?',
            (reqId , ))
        r = c.fetchone()
        if r:
            secType = r[2]
        else:
            # error!
            secType = None
        c.close()
        if secType == 'OPT':
            self.clearRequestIdAndContinue(reqId)
        else:
            self.clearRequestId(reqId)
    # ! [ticksnapshotend]

    @iswrapper
    # reqSecDefOptParams callback
    def securityDefinitionOptionParameter(self, reqId: int, exchange: str,
                                          underlyingConId: int, tradingClass: str, multiplier: str,
                                          expirations: SetOfString, strikes: SetOfFloat):
        super().securityDefinitionOptionParameter(reqId, exchange,
                                                underlyingConId, tradingClass, multiplier, expirations, strikes)
        contract = self.findContractById(self.wheelSymbolsProcessingSymbol)
        if contract.symbol == tradingClass:
            if self.wheelSymbolsExpirations:
                self.wheelSymbolsExpirations = self.wheelSymbolsExpirations.union(expirations)
            else:
                self.wheelSymbolsExpirations = expirations
            if self.wheelSymbolsProcessingStrikes:
                self.wheelSymbolsProcessingStrikes = self.wheelSymbolsProcessingStrikes.union(strikes)
            else:
                self.wheelSymbolsProcessingStrikes = strikes
        else:
            print('securityDefinitionOptionParameter. unsupported tradingClass:', tradingClass,'was expecting:', contract.symbol)
        # if (exchange == "SMART") or ((not self.wheelSymbolsExpirations) and (tradingClass == contract.symbol)):
        #     self.wheelSymbolsExpirations = sorted(expirations)
        #     self.wheelSymbolsProcessingStrikes = sorted(strikes)
            # self.wheelSymbolsProcessingSymbol = tradingClass 09/11/2021 can we do without this? otherwise use underlyingConId to ftech info

    @iswrapper
    def securityDefinitionOptionParameterEnd(self, reqId: int):
        super().securityDefinitionOptionParameterEnd(reqId)
        self.wheelSymbolsExpirations = sorted(self.wheelSymbolsExpirations)
        self.wheelSymbolsProcessingStrikes = sorted(self.wheelSymbolsProcessingStrikes)
        self.clearRequestId(reqId)
        self.processNextOptionExpiration()
    # ! [securityDefinitionOptionParameterEnd]

    @iswrapper
    def openOrder(self, orderId: OrderId, contract: Contract, order: Order,
                  orderState: OrderState):
        super().openOrder(orderId, contract, order, orderState)
        self.getDbConnection()
        c = self.db.cursor()
        # Update OpenOrder table
        t = (orderId, )
        c.execute('SELECT id, contract_id, perm_id FROM open_order WHERE perm_id = ?', (order.permId, ))
        r = c.fetchone()
        if not r:
            portfolio_id = self.findPortfolio(order.account)
            if (contract.secType == 'BAG') and (contract.tradingClass == 'COMB'):
                c.execute('SELECT id FROM contract WHERE con_id = ?', (contract.comboLegs[0].conId, ))
                r = c.fetchone()
                if r:
                    if order.action == contract.comboLegs[0].action:
                        action = 'BUY'
                    else:
                        action = 'SELL'
                    contract_id = r[0]
                    t = (portfolio_id, contract_id, order.permId, order.clientId, orderId, action, order.totalQuantity, order.cashQty, order.lmtPrice, order.auxPrice, orderState.status, order.totalQuantity, )
                    c.execute(
                        'INSERT INTO open_order(account_id, contract_id, perm_id, client_id, order_id, action_type, total_qty, cash_qty, lmt_price, aux_price, status, remaining_qty) ' \
                        'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        t)  # better use permid
                c.execute('SELECT id FROM contract WHERE con_id = ?', (contract.comboLegs[1].conId, ))
                r = c.fetchone()
                if r:
                    if order.action == contract.comboLegs[1].action:
                        action = 'BUY'
                    else:
                        action = 'SELL'
                    contract_id = r[0]
                    t = (portfolio_id, contract_id, order.permId, order.clientId, orderId, action, order.totalQuantity, order.cashQty, order.lmtPrice, order.auxPrice, orderState.status, order.totalQuantity, )
                    c.execute(
                        'INSERT INTO open_order(account_id, contract_id, perm_id, client_id, order_id, action_type, total_qty, cash_qty, lmt_price, aux_price, status, remaining_qty) ' \
                        'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', t)
            else:
                contract_id = self.findOrCreateContract(contract)
                if contract_id:
                    t = (portfolio_id, contract_id, order.permId, order.clientId, orderId, order.action, order.totalQuantity, order.cashQty, order.lmtPrice, order.auxPrice, orderState.status, order.totalQuantity, )
                    c.execute(
                        'INSERT INTO open_order(account_id, contract_id, perm_id, client_id, order_id, action_type, total_qty, cash_qty, lmt_price, aux_price, status, remaining_qty) ' \
                        'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', t)
        else:
            print('order already exists', order, contract)
        c.close()
        self.db.commit()
    # ! [openorder]

    @iswrapper
    def openOrderEnd(self):
        super().openOrderEnd()
        self.ordersLoaded = True
    # ! [openorderend]

    @iswrapper
    def orderStatus(self, orderId: OrderId, status: str, filled: float,
                    remaining: float, avgFillPrice: float, permId: int,
                    parentId: int, lastFillPrice: float, clientId: int,
                    whyHeld: str, mktCapPrice: float):
        super().orderStatus(orderId, status, filled, remaining,
                            avgFillPrice, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice)
        # Update OpenOrder
        self.getDbConnection()
        c = self.db.cursor()
        if status == 'Submitted' or status == 'PreSubmitted':
            c.execute(
                'UPDATE open_order SET status = ?, remaining_qty = ? WHERE perm_id = ?',
                (status, remaining, permId, ))
        elif status == 'Cancelled' or status == 'Filled':
            c.execute('DELETE FROM open_order WHERE perm_id = ?', (permId, ))
        else:
            print('orderStatus. unknow status', status)
        c.close()
        self.db.commit()
    # ! [orderstatus]

    # reqHistoricalData callback
    @iswrapper
    def historicalData(self, reqId:int, bar: BarData):
        super().historicalData(reqId, bar)
#        print("HistoricalData. ReqId:", reqId, "BarData.", bar)
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            'UPDATE stock '
            ' SET Historical_Volatility = ? '
            ' WHERE stock.id = (SELECT contract.id FROM contract where contract.api_req_id = ?)', 
            (round(bar.close, 4), reqId, ))
        if c.rowcount != 1:
            print('historicalData. Error: failed to store volatility.')
        else:
            # print(c.rowcount, 'record(s) updated with historical volatility')
            pass
        c.close()
        self.db.commit()
    # ! [historicaldata]

    # reqHistoricalData callback
    @iswrapper
    def historicalDataEnd(self, reqId: int, start: str, end: str):
        super().historicalDataEnd(reqId, start, end)
#        print("HistoricalDataEnd. ReqId:", reqId, "from", start, "to", end)
        # get contract price, in case it's not in current portfolio
        self.getDbConnection()
        c = self.db.cursor()
        c.execute(
            'SELECT contract.con_id, contract.currency, contract.secType, contract.symbol FROM contract WHERE contract.api_req_id = ?',
            (reqId, ))
        r = c.fetchone()
        c.close()
        if r:
            nextReqId = self.getNextTickerId()
            contract = Contract()
            contract.exchange = 'SMART'
            contract.conId = r[0]
            contract.currency = r[1]
            contract.secType = r[2]
            contract.symbol = r[3]
            c = self.db.cursor()
            c.execute(
                'UPDATE contract SET api_req_id = ? WHERE contract.con_id = ?', 
                (nextReqId, contract.conId, ))
            if c.rowcount != 1:
                print('historicalDataEnd. Contract not found:', contract)
            else:
                # print('triggered api_req_id', nextReqId, 'for', contract.conId)
                pass
            c.close()
            self.db.commit()
            self.reqMktData(nextReqId, contract, "", True, False, [])
        else:
            print('historicalDataEnd. Error: record not found.')
    # ! [historicaldataend]

    @iswrapper
    # reqContractDetails callback
    def contractDetails(self, reqId: int, contractDetails: ContractDetails):
        super().contractDetails(reqId, contractDetails)
        # print('contractDetails.', contractDetails)
        # create contract if not exists
        self.findOrCreateContract(contractDetails.contract)
        if contractDetails.contract.secType == 'STK':
            self.getDbConnection()
            c = self.db.cursor()
            t = (contractDetails.industry, contractDetails.category, contractDetails.subcategory, contractDetails.contract.conId, )
            c.execute('UPDATE stock SET industry = ?, category = ?, subcategory = ? WHERE id = (SELECT id FROM contract WHERE contract.con_id = ?)', t)
            nextReqId = self.getNextTickerId()
            c.execute(
                'UPDATE contract SET api_req_id = ?, name = ?, tick_price = ? WHERE contract.con_id = ?',
                (nextReqId, contractDetails.longName, contractDetails.minTick, contractDetails.contract.conId, ))
            if c.rowcount != 1:
                print('contractDetails. Contract not found:', contractDetails.contract, c.rowcount, 'row(s)')
            else:
                # print('triggered api_req_id', nextReqId, 'for', contractDetails.contract)
                pass
            c.close()
            self.db.commit()
#            print('requesting reqHistoricalData with id', nextReqId, contractDetails.contract)
            #queryTime = (datetime.datetime.today() - datetime.timedelta(days=180)).strftime("%Y%m%d %H:%M:%S")
            #queryTime = (datetime.datetime.today()).strftime("%Y%m%d 00:00:00")
            queryTime = ""
            self.reqHistoricalData(nextReqId, contractDetails.contract, queryTime,
                "2 D", "1 day", "HISTORICAL_VOLATILITY", 0, 1, False, [])
            print('requesting reqSecDefOptParams for', contractDetails.contract)
            self.reqSecDefOptParams(self.getNextTickerId(), contractDetails.contract.symbol, "", contractDetails.contract.secType, contractDetails.contract.conId)
        elif contractDetails.contract.secType == 'OPT':
            nextReqId = self.getNextTickerId()
            self.getDbConnection()
            c = self.db.cursor()
            c.execute(
                'UPDATE contract SET api_req_id = ?, tick_price = ? WHERE contract.con_id = ?', 
                (nextReqId, contractDetails.minTick, contractDetails.contract.conId, ))
            if c.rowcount != 1:
                print('contractDetails. Contract not found:', contractDetails.contract, c.rowcount, 'row(s)')
            else:
                # print('triggered api_req_id', nextReqId, 'for', contractDetails.contract.conId)
                pass
            c.close()
            self.db.commit()
            self.reqMktData(nextReqId, contractDetails.contract, "", True, False, [])
    # ! [contractdetails]

    @iswrapper
    def contractDetailsEnd(self, reqId: int):
        super().contractDetailsEnd(reqId)
        if (self.clearRequestId(reqId) != -1):  # probably always -1
            print('contractDetailsEnd with known id:', reqId)
    # ! [contractdetailsend]

    @iswrapper
    # reqManagedAccts callback
    def managedAccounts(self, accountsList: str):
        super().managedAccounts(accountsList)
        if self.account:
            return
        else:
            # first time
            self.account = accountsList.split(",")[0]

            self.lastCashAdjust = 0
            self.lastNakedPutsSale = 0
            self.lastRollOptionTime = dict()
            self.lastWheelRequestTime = None

            self.clearAllApiReqId()
            if not self.useCache:
                self.clearOptionsPrices()
            self.clearPortfolioBalances(self.account)
            self.clearPortfolioPositions(self.account)
            self.clearOpenOrders(self.account)

            # self.reqMarketDataType(MarketDataTypeEnum.REALTIME)
            # start account updates
            self.reqAccountUpdates(True, self.account)
            # Requesting the next valid id. The parameter is always ignored.
            self.reqIds(-1)
            self.reqOpenOrders()

    @iswrapper
    def accountDownloadEnd(self, accountName: str):
        super().accountDownloadEnd(accountName)
        self.portfolioLoaded = True
    # ! [accountdownloadend]

    @iswrapper
    def updateAccountTime(self, timeStamp: str):
        super().updateAccountTime(timeStamp)
        # print("UpdateAccountTime. Time:", timeStamp)
        if self.started:
            if self.wheelSymbolsProcessingSymbol \
                and (time.time() > (self.lastWheelRequestTime + 30)):
                    self.clearAllApiReqId()
                    if self.wheelSymbolsExpirations and (len(self.wheelSymbolsExpirations) > 0):
                        # symbol in process and no activity for more than 30 secs, we are stuck
                        if self.wheelSymbolsProcessingExpiration:
                            #  so restart with last expiration
                            self.processCurrentOptionExpiration()
                        else:
                            self.processNextOptionExpiration()
                    else:
                        # This should not happen, but once we got wheelSymbolsProcessingStrikes clear, don't know how
                        self.selectNextSymbol()
            elif (time.time() > (self.lastWheelProcess + self.getFindSymbolsSleep(self.account))) \
                and (self.wheelSymbolsProcessingSymbol == None) \
                and (len(self.wheelSymbolsToProcess) == 0):
                    # no process in progress, start over
                    self.selectNextSymbol()
            # perform regular tasks
            self.sellShortPutsIfPossible()
            self.adjustCash()
    # ! [updateaccounttime]

    @iswrapper
    def updateAccountValue(self, key: str, val: str, currency: str,
                           accountName: str):
        super().updateAccountValue(key, val, currency, accountName)
        self.getDbConnection()
        if (key == 'CashBalance') and (currency != 'BASE'):
            # update currency cash value
            c = self.db.cursor()
            id = self.findPortfolio(accountName)
            t = (val, id, currency)
            #print(t)
            c.execute('UPDATE balance SET quantity = ? WHERE portfolio_id = ? AND currency = ?', t)
            #print(c.rowcount)
            if (c.rowcount == 0) and (val != 0):
                c.execute('INSERT INTO balance(quantity, portfolio_id, currency) VALUES (?, ?, ?)', t)
            c.close()
            self.db.commit()
        elif (key == 'ExchangeRate') and (currency != 'BASE'):
            # update exchange rate
            c = self.db.cursor()
            t = (accountName, )
            c.execute('SELECT id, base_currency FROM portfolio WHERE account = ?', t)
            r = c.fetchone()
            #print(r)
            id = r[0]
            base = r[1]
            t = (val, base, currency)
            c.execute('UPDATE currency SET rate = 1.0/? WHERE base = ? AND currency = ?', t)
            #print(c.rowcount)
            if (c.rowcount == 0):
                c.execute('INSERT INTO currency(rate, base, currency) VALUES (1.0/?, ?, ?)', t)
            t = (val, currency, base)
            c.execute('UPDATE currency SET rate = ? WHERE base = ? AND currency = ?', t)
            #print(c.rowcount)
            if (c.rowcount == 0):
                c.execute('INSERT INTO currency(rate, base, currency) VALUES (?, ?, ?)', t)
            c.close()
            self.db.commit()
        elif (key == 'NetLiquidationByCurrency') and (currency == 'BASE'):
            self.portfolioNAV = float(val)
    # ! [updateaccountvalue]

    @iswrapper
    def updatePortfolio(self, contract: Contract, position: float,
                        marketPrice: float, marketValue: float,
                        averageCost: float, unrealizedPNL: float,
                        realizedPNL: float, accountName: str):
#        print("updatePortfolio.", "Symbol:", contract.symbol, "SecType:", contract.secType, "Exchange:",
#              contract.exchange, "Position:", position, "MarketPrice:", marketPrice,
#              "MarketValue:", marketValue, "AverageCost:", averageCost,
#              "UnrealizedPNL:", unrealizedPNL, "RealizedPNL:", realizedPNL,
#              "AccountName:", accountName)
        super().updatePortfolio(contract, position, marketPrice, marketValue,
                                averageCost, unrealizedPNL, realizedPNL, accountName)
        if (contract.secType != 'UNK') and (contract.secType != 'CASH'):
            # very surprising, contract.strike is correct inside position callback, but is in pence in updatePortfolio callback
            if contract.currency == 'GBP':
                contract.strike /= 100.0
            self.findOrCreateContract(contract)
            self.createOrUpdatePosition(contract, position, averageCost, accountName)
            self.getDbConnection()
            c = self.db.cursor()
            c.execute(
                'UPDATE contract SET price = ?, updated = datetime(\'now\') WHERE contract.con_id = ?', 
                (marketPrice, contract.conId, ))
            c.close()
            self.db.commit()
            if (contract.secType == 'STK'):
                self.sellCoveredCallsIfPossible(contract, position, marketPrice, marketValue,
                    averageCost, unrealizedPNL, realizedPNL, accountName)
            elif (contract.secType == 'OPT') and contract.lastTradeDateOrContractMonth:
                self.rollOptionIfNeeded(contract, position, marketPrice, marketValue,
                    averageCost, unrealizedPNL, realizedPNL, accountName)
    # ! [updateportfolio]

    """
    Trading functions
    """

    def adjustCash(self):
        if (not self.portfolioLoaded) or (not self.ordersLoaded) or (not self.optionContractsAvailable) or (not self.getCashStrategy(self.account)):
            return
        sleep = self.getAdjustCashSleep(self.account)
        seconds = time.time()
        if (seconds < (self.lastCashAdjust + sleep)):
            return

        benchmark = self.getBenchmark(self.account)
        benchmarkSymbol = benchmark.symbol
        benchmarkCurrency = benchmark.currency
        benchmarkPrice = self.getSymbolPrice(benchmarkSymbol)
        benchmarkPriceInBase = self.getSymbolPriceInBase(self.account, benchmarkSymbol)
        benchmarkUnits = self.getPortfolioStocksQuantity(self.account, benchmarkSymbol)
        benchmarkCurrencyBalance = self.getCurrencyBalance(self.account, benchmarkCurrency)
        benchmarkBalance = self.getCurrencyBalanceInBase(self.account, benchmarkCurrency)
        benchmarkBaseToCurrencyRatio = self.getBaseToCurrencyRate(self.account, benchmarkCurrency)

        strategy = self.getCashStrategy(self.account)
        if not strategy:
            extra_cash = 0
        elif strategy == 1:
            extra_cash = benchmarkCurrencyBalance
        elif strategy == 2:
            # total Options portfolio value
            required_cover = self.getPortfolioOptionsValue(self.account, None)
            # *optionnal* if we don't want to have a big call position pulling our cash balance negative
            required_cover = max(required_cover, 0)
            # cash amount that can be invested in benchmark
            extra_cash = self.getTotalCashAmount(self.account) - required_cover
        elif strategy == 3:
            # init required_cover
            required_cover = 0
            # how much do we need to cover short put?
            required_cover -= self.getWeightedShortedOptionsAmountInBase(self.account, 'P')
            # how much could we get from short call?
            required_cover -= self.getWeightedShortedOptionsAmountInBase(self.account, 'C')
            # *optionnal* if we don't want to have a big call position pulling our cash balance negative
            required_cover = max(required_cover, 0)
            # cash amount that can be invested in benchmark
            extra_cash = self.getTotalCashAmount(self.account) - required_cover
        else:
            print('cash strategy not implemented!!!')
            extra_cash = 0

        # print('required_cover:', required_cover, 'extra_cash:', extra_cash)
        if extra_cash > benchmarkPriceInBase:
            # all good, our cash is higher than the cover needed for short options
            # we can invest some cash, but no more than benchmark balance
            x = min(extra_cash, benchmarkBalance)
            to_sell = 0
            to_buy = math.floor(x / benchmarkPriceInBase)
        elif extra_cash < -benchmarkPriceInBase:
            # we should sell something, but no more than available benchmark units
            x = math.ceil(-extra_cash / benchmarkPriceInBase)
            to_sell = min(x, benchmarkUnits)
            to_buy = 0
        else:
            to_sell = 0
            to_buy = 0

        # open orders quantity
        benchmark_on_buy = self.getStockQuantityOnOrderBook(self.account, benchmarkSymbol, 'BUY')
        benchmark_on_buy -= self.getOptionsQuantityOnOrderBook(self.account, benchmarkSymbol, 'P', 'SELL')
        benchmark_on_sale = self.getStockQuantityOnOrderBook(self.account, benchmarkSymbol, 'SELL')
        benchmark_on_sale -= self.getOptionsQuantityOnOrderBook(self.account, benchmarkSymbol, 'C', 'SELL')

        if strategy and ((to_buy - to_sell) != (benchmark_on_buy + benchmark_on_sale)):
            # order required
            self.lastCashAdjust = seconds
            self.cancelStockOrderBook(self.account, benchmarkSymbol, 'BUY')
            self.cancelStockOrderBook(self.account, benchmarkSymbol, 'SELL')
            to_adjust = to_buy - to_sell + self.getOptionsQuantityOnOrderBook(self.account, benchmarkSymbol, 'P', 'SELL')
            if (to_adjust >= 10):   # buy at least 10 unit
                print('to buy:', to_adjust)
                self.placeOrder(self.nextOrderId(), benchmark, TraderOrder.BuyBenchmark(to_adjust))
            elif (to_adjust <= -2): # sell at least 2 unit
                print('to sell:', -to_adjust)
                self.placeOrder(self.nextOrderId(), benchmark, TraderOrder.SellBenchmark(-to_adjust))

    def sellShortPutsIfPossible(self):
        if (not self.portfolioLoaded) or (not self.ordersLoaded) or (not self.optionContractsAvailable):
            return
        sleep = self.getNakedPutSleep(self.account)
        seconds = time.time()
        if (seconds < (self.lastNakedPutsSale + sleep)):
            return
        self.lastNakedPutsSale = seconds

        portfolio_nav = self.getTotalCashAmount(self.account)
        portfolio_nav += self.getPortfolioStocksValue(self.account, None)
        portfolio_nav += self.getPortfolioOptionsValue(self.account, None)
        
        puttable_amount = self.getTotalCashAmount(self.account)
        puttable_amount += self.getBenchmarkAmountInBase(self.account)
        puttable_amount *= self.getNakedPutRatio(self.account)
        puttable_amount += self.getTotalNakedPutAmount(self.account)
        puttable_amount += self.getOptionsAmountOnOrderBook(self.account, None, 'P', 'SELL')
        print('puttable_amount:', puttable_amount)

        if puttable_amount > 0:
            # select option contracts which match:
            #   implied volatility > historical volatility
            #   Put
            #   OTM
            #   strike < how much we can engage
            #   at least 80% success (delta >= -0.2)
            #   premium at least $0.25
            t = ('P', puttable_amount/100, -(1 - self.getNakedPutWinRatio(self.account)), self.getMinPremium(self.account), )
            self.getDbConnection()
            c = self.db.cursor()
            c.execute("""
                SELECT contract.con_id,
                  stock_contract.symbol, option.last_trade_date, option.strike, option.call_or_put, contract.symbol,
                  (julianday(option.last_trade_date) - julianday('now') + 1) dte, contract.bid / option.strike / (julianday(option.last_trade_date) - julianday('now') + 1) * 360,
                  contract.bid, contract.ask, stock_contract.price, option.implied_volatility, stock.historical_volatility, option.delta
                FROM contract, option, stock, contract stock_contract
                WHERE option.id = contract.id
                  AND stock.id = option.stock_id
                  AND stock_contract.id = stock.id
                  AND option.call_or_put = ?
                  AND option.implied_volatility > stock.historical_volatility
                  AND option.strike < stock_contract.price
                  AND option.strike < ?
                  AND option.delta >= ?
                  AND contract.bid >= ?
                  AND stock_contract.price < stock_contract.previous_close_price
                  AND julianday(option.last_trade_date) > julianday('now')
                """, t)
            opt = c.fetchall()
            c.close()
            print(len(opt), 'contracts')
            # sort by annualized yield descending
            for rec in sorted(opt, key=cmp_to_key(lambda item1, item2: item2[7] - item1[7])):
                # verify that this symbol is in our wheel
                nav_ratio = self.getWheelSymbolNavRatio(self.account, rec[1])
                if nav_ratio:
                    already_engaged = self.getPortfolioStocksValue(self.account, rec[1]) - self.getNakedPutAmount(self.account, rec[1]) - self.getOptionsAmountOnOrderBook(self.account, rec[1], 'P', 'SELL')
                    engaged_with_put = already_engaged + (rec[3] * 100 / self.getBaseToCurrencyRate(self.account, 'USD'))
                    if engaged_with_put <= (portfolio_nav * nav_ratio):
                        askprice = round(rec[9], 2)
                        bidprice = round(rec[8], 2)
                        midprice = round((rec[8] + rec[9]) / 2, 2)
                        print(
                            'Placing order for', rec[5],
                            round(already_engaged, 2), round(already_engaged / portfolio_nav * 100, 1), '% engaged for stock and',
                            round(engaged_with_put, 2), round(engaged_with_put / portfolio_nav * 100, 1), '% engaged with this Put',
                            'of delta', round(rec[13], 2),
                            'and expected yield of', round(rec[7] * 100, 1), '%',
                            'with underlying price of', round(rec[10], 2),
                            'and bid price of', bidprice,
                            'and ask price of', askprice,
                            'with DTE of', round(rec[6], 1),
                            'with option implied vol. of', round(rec[11] * 100, 0), '%',
                            'and stock historycal vol. of', round(rec[12] * 100, 0), '%'
                            )
                        contract = Contract()
                        contract.secType = "OPT"
                        contract.currency = 'USD'
                        contract.exchange = "SMART"
                        contract.conId = rec[0]
                        contract.symbol = rec[1]
                        contract.lastTradeDateOrContractMonth = rec[2].replace('-', '')
                        contract.strike = rec[3]
                        contract.right = rec[4]
                        contract.multiplier = "100"
                        self.placeOrder(self.nextOrderId(), contract, TraderOrder.SellNakedPut(askprice))
                        # stop after first submitted order
                        break
                    else:
                        print(
                            rec[5], 'ignored.',
                            round(already_engaged / portfolio_nav * 100, 1), '% engaged for this stock and',
                            round(engaged_with_put / portfolio_nav * 100, 1), '% would be engaged with this Put of expected yield of', round(rec[7] * 100, 1), '%')
                else:
                    print(rec[5], 'stopped.')

    def sellCoveredCallsIfPossible(self,
            contract: Contract, position: float,
            marketPrice: float, marketValue: float,
            averageCost: float, unrealizedPNL: float,
            realizedPNL: float, accountName: str):
        id = self.findOrCreateContract(contract)
        if (not self.ordersLoaded) \
            or (not id in self.wheelSymbolsProcessed) \
            or (contract.secType != 'STK') or (position < 100) \
            or (contract.currency != 'USD'):
            return
        # print('sellCoveredCallsIfPossible.', 'contract:', contract)
        if (contract.secType == 'STK'):
            stocks_on_sale = self.getStockQuantityOnOrderBook(accountName, contract.symbol, 'SELL')
            short_call_position = self.getShortCallPositionQuantity(accountName, contract.symbol)
            call_on_order_book = self.getOptionsQuantityOnOrderBook(accountName, contract.symbol, 'C', 'SELL')
            net_pos = position + stocks_on_sale + short_call_position + call_on_order_book
            # print('net_pos:', net_pos)
            if net_pos >= 100:
                # select option contracts which match:
                #   Call
                #   OTM
                #   strike > PRU
                #   at least 85% success (delta <= 0.15)
                #   premium at least $0.25
                #   underlying stock is current stock
                t = ('C', averageCost, (1 - self.getShortCallWinRatio(self.account)), self.getMinPremium(self.account), contract.conId, )
                self.getDbConnection()
                c = self.db.cursor()
                c.execute("""
                    SELECT contract.con_id,
                      stock_contract.symbol, option.last_trade_date, option.strike, option.call_or_put, contract.symbol,
                      julianday(option.last_trade_date) - julianday('now') + 1, contract.bid / option.strike / (julianday(option.last_trade_date) - julianday('now') + 1) * 360,
                      contract.bid, contract.ask, stock_contract.price, option.implied_volatility, stock.historical_volatility, option.delta
                     FROM contract, option, stock, contract stock_contract
                     WHERE option.id = contract.id
                      AND stock.id = option.stock_id
                      AND stock_contract.id = stock.id
                      AND option.call_or_put = ?
                      AND option.strike > stock_contract.price
                      AND option.strike > ?
                      AND option.delta <= ?
                      AND contract.bid >= ?
                      AND stock_contract.con_id = ?
                      AND stock_contract.price > stock_contract.previous_close_price
                    """, t)
                # sort by annualized yield descending
                opt = sorted(c.fetchall(), key=cmp_to_key(lambda item1, item2: item2[7] - item1[7]))
                c.close()
                print(len(opt), 'possible Call contracts to sell for', contract)
                if len(opt) > 0:
#                    for rec in opt:
#                        print(rec)
                    rec = opt[0]
                    bidprice = round(rec[8], 2)
                    askprice = round(rec[9], 2)
                    midprice = round((bidprice + askprice) / 2, 2)
                    print(
                        'Placing order for', rec[5],
                        'of delta', round(rec[13], 2),
                        'and expected yield of', round(rec[7] * 100, 1), '%',
                        'with underlying price of', round(rec[10], 2),
                        'and bid price of', bidprice,
                        'and ask price of', askprice,
                        'with DTE of', round(rec[6], 1),
                        'with option implied vol. of', round(rec[11] * 100, 0), '%',
                        'and stock historycal vol. of', round(rec[12] * 100, 0), '%'
                        )
                    contract = Contract()
                    contract.conId = rec[0]
                    contract.secType = "OPT"
                    contract.currency = 'USD'
                    contract.exchange = "SMART"
                    contract.symbol = rec[1]
                    contract.lastTradeDateOrContractMonth = rec[2].replace('-', '')
                    contract.strike = rec[3]
                    contract.right = rec[4]
                    contract.multiplier = "100"
                    # print(price)
                    self.placeOrder(self.nextOrderId(), contract, TraderOrder.SellCoveredCall(askprice, math.floor(net_pos/100)))
        # print('sellCoveredCallsIfPossible done.')

    @staticmethod
    def OptionComboContract(underlying: str, buyleg: int, sellleg: int):
        #! [bagoptcontract]
        contract = Contract()
        contract.symbol = underlying
        contract.secType = "BAG"
        contract.currency = "USD"
        contract.exchange = "SMART"

        leg1 = ComboLeg()
        leg1.conId = buyleg
        leg1.ratio = 1
        leg1.action = "BUY"
        leg1.exchange = "SMART"

        leg2 = ComboLeg()
        leg2.conId = sellleg
        leg2.ratio = 1
        leg2.action = "SELL"
        leg2.exchange = "SMART"

        contract.comboLegs = []
        contract.comboLegs.append(leg1)
        contract.comboLegs.append(leg2)
        #! [bagoptcontract]
        return contract

    def rollOptionIfNeeded(self,
            contract: Contract, position: float,
            marketPrice: float, marketValue: float,
            averageCost: float, unrealizedPNL: float,
            realizedPNL: float, accountName: str):
        # print("rollOptionIfNeeded.", "Symbol:", contract.symbol, "SecType:", contract.secType, "Exchange:", contract.exchange, "Position:", position, "MarketPrice:", marketPrice, "MarketValue:", marketValue, "AverageCost:", averageCost, "UnrealizedPNL:", unrealizedPNL, "RealizedPNL:", realizedPNL, "AccountName:", accountName)
        if contract.conId not in self.lastRollOptionTime:
            self.lastRollOptionTime[contract.conId] = 0
        seconds = time.time()
        expiration = datetime.datetime.strptime(contract.lastTradeDateOrContractMonth, '%Y%m%d').timestamp()
        hours = (expiration - seconds) / 3600
        if (not self.ordersLoaded) \
            or (not position) or (hours > ((24 * self.getRollDaysBefore(accountName)) - 12)) \
            or (seconds < (self.lastRollOptionTime[contract.conId] + self.getRollOptionsSleep(accountName))):
            return
        id = self.findOrCreateContract(self.findContractBySymbol(contract.symbol))
        if (not id in self.wheelSymbolsProcessed):
            return
        position += self.getContractQuantityOnOrderBook(accountName, contract, 'BUY')
        if (position < 0):
            # print('rollOptionIfNeeded.', 'contract:', contract, 'net position:', position)
            underlying_price = self.getUnderlyingPrice(contract)
            if (contract.right == 'C' and underlying_price > contract.strike):
                print('need to roll ITM Call', contract)
                self.lastRollOptionTime[contract.conId] = seconds
                # search for replacement contracts
                # select option contracts which match:
                #   same right (Call/Call)
                #   strike >= underlying price, no could be difficult sometimes, only strike >= current strike for the moment (but we will keep highest strike)
                #   maturity > current maturity
                #   same underlying stock
                #   bid >= current ask
                # the trade is going against us,
                # we will try to exit as soon as possible: closest expiration
                # and select the highest strike, most onservative approach
                # simple rules, maybe could be improved by using delta and yield (same rules as position openning)?
                self.getDbConnection()
                c = self.db.cursor()
                c.execute(
                    """
                    SELECT contract.con_id,
                      option.last_trade_date, option.strike, option.call_or_put, contract.symbol,
                      (julianday(option.last_trade_date) - julianday(option_ref.last_trade_date) + 1) dte,
                      ((contract.bid - contract_ref.ask) / option.strike / (julianday(option.last_trade_date) - julianday(option_ref.last_trade_date) + 1) * 365.25) yield,
                      contract.bid, contract.ask, option.delta delta, contract_ref.bid, contract_ref.ask, contract_ref.price
                     FROM contract, option, contract contract_ref, option option_ref
                     WHERE contract_ref.con_id = ?
                      AND contract_ref.id = option_ref.id
                      AND option_ref.stock_id = option.stock_id
                      AND contract.id = option.id
                      AND option.last_trade_date > option_ref.last_trade_date
                      AND contract_ref.ask <= contract.bid
                      AND option_ref.call_or_put = option.call_or_put
                      AND option.strike > option_ref.strike
                     ORDER BY dte ASC, delta ASC
                    """,
                    (contract.conId, ))
                opt = c.fetchall()
                c.close()
                print(len(opt), 'possible contracts')
                if len(opt) > 0:
                    # sort by delta
                    # opt = sorted(opt, key=cmp_to_key(lambda item1, item2: item1[9] - item2[9]))
                    # default is to select lowest risky contract
                    c = opt[0]
                    conId = c[0]
                    min_price = c[7] - c[11]
                    max_price = c[8] - c[11]

                    # # unless we can find a high yielding one with
                    # # strike >= underlying price
                    # # delta < (1 - succes ratio)
                    # delta = (1 - self.getShortCallWinRatio(accountName))
                    # # opt = sorted(opt, key=cmp_to_key(lambda item1, item2: item1[6] - item2[6]))
                    # for c in opt:
                    #     print(c)
                    #     if (c[2] > underlying_price) and (c[9] <= delta):
                    #         conId = c[0]
                    #         min_price = c[7] - c[11]
                    #         max_price = c[8] - c[11]
                    #         break

                    # place order
                    price = round((min_price + max_price) / 2, 2)
                    self.placeOrder(self.nextOrderId(),
                        self.OptionComboContract(contract.symbol, conId, contract.conId),
                        TraderOrder.ComboLimitOrder("SELL", -position, price, False))
            elif (contract.right == 'P' and underlying_price < contract.strike):
                print('Need to roll ITM Put', contract)
                self.lastRollOptionTime[contract.conId] = seconds
                # search for replacement contracts
                # select option contracts which match:
                #   same right (Call/Call)
                #   strike < underlying price
                #   maturity > current maturity
                #   same underlying stock
                #   bid > current ask, > and not >= to handle the 0 ask/bid case (???)
                self.getDbConnection()
                c = self.db.cursor()
                c.execute(
                    """
                    SELECT contract.con_id,
                      option.last_trade_date, option.strike, option.call_or_put, contract.symbol,
                      julianday(option.last_trade_date) - julianday(option_ref.last_trade_date) + 1, (contract.bid - contract_ref.ask) / option.strike / (julianday(option.last_trade_date) - julianday(option_ref.last_trade_date) + 1) * 360,
                      contract.bid, contract.ask, option.delta, contract_ref.bid, contract_ref.ask, contract_ref.price
                     FROM contract, option, contract contract_ref, option option_ref
                     WHERE contract_ref.con_id = ?
                      AND contract_ref.id = option_ref.id
                      AND option_ref.stock_id = option.stock_id
                      AND contract.id = option.id
                      AND option.last_trade_date > option_ref.last_trade_date
                      AND contract_ref.ask < contract.bid
                      AND option_ref.call_or_put = option.call_or_put
                      AND option.strike <= option_ref.strike
                      AND option.delta NOTNULL
                     ORDER BY option.last_trade_date ASC, option.strike ASC
                    """,
                    (contract.conId, ))
                opt = c.fetchall()
                c.close()
                opt = sorted(opt, key=cmp_to_key(lambda item1, item2: item2[9] - item1[9]))
                print(len(opt), 'possible contracts, by delta:')
                for c in opt:
                    print(c)
                if (len(opt) >= 1):
                    min_price = round(opt[0][7] - opt[0][11], 2)
                    max_price = round(opt[0][8] - opt[0][10], 2)
                    price = round((min_price + max_price) / 2, 2)
                    # print('min_price', min_price, 'price', price, 'max_price', max_price)
                    self.placeOrder(self.nextOrderId(),
                        self.OptionComboContract(contract.symbol, opt[0][0], contract.conId),
                        TraderOrder.ComboLimitOrder("SELL", -position, price, False))

    def processCurrentOptionExpiration(self):
        # print('processCurrentOptionExpiration.')
        self.lastWheelRequestTime = time.time()
        exp = self.wheelSymbolsProcessingExpiration
        expiration = datetime.date(int(exp[0:4]), int(exp[4:6]), int(exp[6:8]))
        if not self.optionContractsAvailable:
            # better to void data than using old values, on first scan as they can be quite old
            self.getDbConnection()
            c = self.db.cursor()
            c.execute(
                """
                UPDATE contract
                SET ask = NULL, price = NULL, bid = NULL, previous_close_price = NULL, ask_date = NULL, updated = NULL, bid_date = NULL
                WHERE contract.id IN (
                    SELECT option.id
                        FROM option, contract stock
                        WHERE option.stock_id = stock.id
                            AND stock.id = ?
                            AND option.last_trade_date = ?
                    )
                """,
                (self.wheelSymbolsProcessingSymbol, expiration, ))
            c.close()
            self.db.commit()
        # price = self.getSymbolPrice(self.wheelSymbolsProcessingSymbol[0])
        contract = self.findContractById(self.wheelSymbolsProcessingSymbol)
        contract.conId = 0
        if contract.primaryExchange == 'EBS':
            contract.exchange = 'SOFFEX'
        contract.primaryExchange = ''
        contract.secType = 'OPT'
        contract.lastTradeDateOrContractMonth = exp
        price = self.getContractPrice(contract)
        num_requests = 0
        if price:
            # atm: first strike index above price
            for atm in range(len(self.wheelSymbolsProcessingStrikes)):
                if self.wheelSymbolsProcessingStrikes[atm] >= price:
                    break
        else:
            atm = math.ceil(len(self.wheelSymbolsProcessingStrikes) / 2)
    #            print('atm:', atm)
        # process at most xx strikes in each direction
        # should be 24 but as reqContractDetails callback will submit new request we will 
        # potentially overcome de 100 simultaneous requests limit
        # I lower substencially as (maybe) TWS is running it's own querie that counts for the same limit
        for i in range(20):
            if (atm-i-1) >= 0:
                # self.reqContractDetailsProcessingCount += 1
                contract.strike = self.wheelSymbolsProcessingStrikes[atm-i-1]
                contract.right = 'P'
                self.reqContractDetails(self.getNextTickerId(), contract)
                num_requests += 1

                # self.reqContractDetailsProcessingCount += 1
                contract.right = 'C'
                self.reqContractDetails(self.getNextTickerId(), contract)
                num_requests += 1

            if (atm+i) < len(self.wheelSymbolsProcessingStrikes):
                # self.reqContractDetailsProcessingCount += 1
                contract.strike = self.wheelSymbolsProcessingStrikes[atm+i]
                contract.right = 'C'
                self.reqContractDetails(self.getNextTickerId(), contract)
                num_requests += 1

                # self.reqContractDetailsProcessingCount += 1
                contract.right = 'P'
                self.reqContractDetails(self.getNextTickerId(), contract)
                num_requests += 1

    def processNextOptionExpiration(self):
        # print('processNextOptionExpiration.')
        self.lastWheelRequestTime = time.time()
        # We are processing all strikes for each expiration for one stock
        # self.wheelSymbolsProcessingSymbol: stock symbol being processed
        # self.wheelSymbolsProcessingStrikes: strikes list associated to process
        # self.wheelSymbolsProcessingExpirations: expirations list associated to process, one at a time
        today = date.today()
        self.wheelSymbolsProcessingExpiration = None
        while self.wheelSymbolsExpirations and (len(self.wheelSymbolsExpirations) > 0):
            exp = self.wheelSymbolsExpirations.pop(0)
            expiration = datetime.date(int(exp[0:4]), int(exp[4:6]), int(exp[6:8]))
            if (expiration - today).days < self.getCrawlDaysNumber(self.account):
                self.wheelSymbolsProcessingExpiration = exp
                break
        if self.wheelSymbolsProcessingExpiration != None:
            self.processCurrentOptionExpiration()
        else:
            print('done with symbol', self.findContractById(self.wheelSymbolsProcessingSymbol).symbol, len(self.wheelSymbolsToProcess), 'left')
            # we are finished with this symbol
            self.wheelSymbolsProcessed.append(self.wheelSymbolsProcessingSymbol)
            self.wheelSymbolsProcessingSymbol = None
            self.wheelSymbolsProcessingStrikes = []
            self.wheelSymbolsExpirations = []
            if (len(self.wheelSymbolsToProcess) > 0):
                self.selectNextSymbol()
            else:
                print('processNextOptionExpiration. All done!')
                # we are done for some
                self.lastWheelProcess = time.time()
                self.optionContractsAvailable = True

    def selectNextSymbol(self):
        # print('selectNextSymbol.')
        self.lastWheelRequestTime = time.time()
        if len(self.wheelSymbolsToProcess) == 0:
            self.wheelSymbolsToProcess = self.getWheelStocksToProcess()
            if self.useCache:
                self.wheelSymbolsProcessed = self.wheelSymbolsToProcess.copy()
        self.wheelSymbolsProcessingSymbol = self.wheelSymbolsToProcess.pop(0)
        print('processing symbol', self.findContractById(self.wheelSymbolsProcessingSymbol).symbol)
        contract = self.findContractById(self.wheelSymbolsProcessingSymbol)
        # price = self.getContractPrice(contract)
        # if price and contract.conId:
        #     self.reqContractDetails(self.getNextTickerId(), contract)
        # else:
        #     # skip this symbol if we don't have price information, will be filled later
        #     self.selectNextSymbol()
        self.reqContractDetails(self.getNextTickerId(), contract)

    """
    Main Program
    """

    @iswrapper
    def connectAck(self):
        if self.asynchronous:
            self.startApi()
    # ! [connectack]

    @iswrapper
    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        self.nextValidOrderId = orderId
        print("NextValidId:", orderId)
        # we can start now
        self.start()
    # ! [nextvalidid]

    @printWhenExecuting
    def start(self):
        if self.started:
            return
        self.started = True
        # first retrieve account info
        self.reqManagedAccts()

    @printWhenExecuting
    def keyboardInterrupt(self):
        self.nKeybInt += 1
        if self.nKeybInt == 1:
            self.stop()
        else:
            print("Finishing test")
            self.done = True

    @printWhenExecuting
    def stop(self):
        # ! [cancelaaccountupdates]
        self.reqAccountUpdates(False, self.account)
        # ! [cancelaaccountupdates]
        self.clearAllApiReqId()
        if (self.db):
            self.db.close()
