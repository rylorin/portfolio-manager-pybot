"""
TBD
"""

import os.path
import logging
import argparse
import time
import datetime

from Trader import Trader

def SetupLogger():
    if os.path.exists("../db/var/log"):
        logfile = time.strftime("../db/var/log/pyibapi.%Y%m%d_%H%M%S.log")
    else:
        if not os.path.exists("log"):
            os.makedirs("log")
        logfile = time.strftime("log/pyibapi.%y%m%d_%H%M%S.log")

    time.strftime("pyibapi.%Y%m%d_%H%M%S.log")

    recfmt = '(%(threadName)s) %(asctime)s.%(msecs)03d %(levelname)s %(filename)s:%(lineno)d %(message)s'

    timefmt = '%y%m%d_%H:%M:%S'

    # logging.basicConfig( level=logging.DEBUG,
    #                    format=recfmt, datefmt=timefmt)
    # RYL logging.basicConfig(filename=time.strftime("log/pyibapi.%y%m%d_%H%M%S.log"),
    logging.basicConfig(filename=logfile,
                        filemode="w",
                        level=logging.INFO,
                        format=recfmt, datefmt=timefmt)
    logger = logging.getLogger()
    console = logging.StreamHandler()
    console.setLevel(logging.ERROR)
    logger.addHandler(console)
#    print("SetupLogger done.")


def main():
    SetupLogger()
    logging.debug("now is %s", datetime.datetime.now())
    logging.getLogger().setLevel(logging.ERROR) # INFO to get API traces

    cmdLineParser = argparse.ArgumentParser("Options trading bot")
    cmdLineParser.add_argument("--cache", action='store_true', dest="use_cache", help = "use previously stored options prices")
    cmdLineParser.add_argument("--client", action="store", type=int,
                               dest="clientId", default=0, help="The ClientId to use")
    cmdLineParser.add_argument("-p", "--port", action="store", type=int,
                               dest="port", default=7497, help="The TCP port to use")
    cmdLineParser.add_argument("--host", action="store",
                               dest="host", default="localhost", help="The IB TWS hostname to use")
    args = cmdLineParser.parse_args()
    print("Using args", args)
    logging.debug("Using args %s", args)
    # print(args)

    try:
        app = Trader()
        # print(args.use_cache)
        app.setUseCache(args.use_cache)
        # ! [connect]
        app.connect(args.host, args.port, args.clientId)
        # ! [connect]
        print("serverVersion:%s connectionTime:%s" % (app.serverVersion(), app.twsConnectionTime()))
        # ! [clientrun]
        app.run()
        # ! [clientrun]
    except:
        raise

if __name__ == "__main__":
    main()
