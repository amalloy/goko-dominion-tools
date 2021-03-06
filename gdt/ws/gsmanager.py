import threading
import time
import logging
import requests
import os
from PIL import Image
import tornado.ioloop
import math
import datetime
from trueskill import Rating

from gdt.util.sync import synchronized
from gdt.model import db_manager
from gdt.ratings.gdt_trueskill import goko_env
from gdt.ratings.gdt_trueskill import rate

from gdt.ratings.rating_system import isotropish

# For synchronization. Can be acquired multiple times by the same thread, but a
# second thread has to wait.
lock = threading.RLock()


# Logic for the GokoSalvager server.  Uses the GSInterface for convenience.
#
# All methods are synchronized, but messages from players can still get lost or
# cross with messages from the server, or can get lost or arrive out of order.
# This class is responsible for all of the edge cases and race conditions.
# More specific classes (e.g. automatch Match objects) should be able to assume
# that they're living in a single-threaded world.
#
class GSManager():

    def __init__(self, gsinterface):
        self.interface = gsinterface
        self.clients = set()
        self.avatar_table = None

        # Check for ratings changes every 3 seonds.  Notify clients.
        (self.iso_table, self.iso_latest) = db_manager.get_all_ratings_by_id()
        tornado.ioloop.PeriodicCallback(self.check_iso_levels, 3000).start()

    @synchronized(lock)
    def addClient(self, client):
        self.clients.add(client)
        user = {
            'playerId': client.playerId,
            'connId': id(client.conn),
            'playerName': client.playerName,
            'version': client.version
        }
        self.interface.sendToAllClients('ADD_EXTUSER', user=user)

    @synchronized(lock)
    def remClient(self, client):
        self.clients.remove(client)
        user = {
            'playerId': client.playerId,
            'connId': id(client.conn),
            'playerName': client.playerName,
            'version': client.version
        }
        self.interface.sendToAllClients('REM_EXTUSER', user=user)
        # TODO: clean up client's remaining dependencies

    @synchronized(lock)
    def receiveFromClient(self, client, msgtype, msgid, message):
        logging.debug(msgtype, msgid, message)

        if msgtype == 'SUBMIT_BLACKLIST':
            db_manager.store_blacklist(client.playerId, message['blacklist'],
                                       message['merge'])

        elif msgtype == 'SUBMIT_PRO_RATING':
            time = datetime.datetime.now()
            db_manager.record_pro_rating(message['playerId'],
                                         message['mu'], message['sigma'],
                                         message['displayed'])

        elif msgtype == 'SUBMIT_PRO_RATINGS':
            time = datetime.datetime.now()
            for i in range(len(message['playerIds'])):
                playerId = message['playerIds'][i]
                mu = message['ratings'][i]['mu']
                sigma = message['ratings'][i]['sigma']
                displayed = message['ratings'][i]['displayed']
                db_manager.record_pro_rating(playerId, mu, sigma, displayed)

        elif msgtype == 'QUERY_EXTUSERS':
            out = []
            for c in self.clients:
                out.append({
                    'connId': id(c.conn),
                    'playerId': c.playerId,
                    'playerName': c.playerName,
                    'version': c.version
                })
            self.interface.respondToClient(client, msgtype, msgid,
                                           clientlist=out)

        elif msgtype == 'QUERY_BLACKLIST':
            blist = db_manager.fetch_blacklist(client.playerId)
            self.interface.respondToClient(client, msgtype, msgid,
                                           blacklist=blist)

        elif msgtype == 'QUERY_BLACKLIST_COMMON':
            cbl = db_manager.fetch_blacklist_common(message['percentile'])
            self.interface.respondToClient(client, msgtype, msgid,
                                           common_blacklist=cbl)

        # TODO: clean up this logic
        elif msgtype == 'QUERY_ISO_RATINGS':
            pids = message['playerIds']
            out = []
            for i in range(len(pids)):
                pid = pids[i]
                rating = db_manager.get_rating_by_id(pid)
                if rating is None:
                    # TODO: Ask client for player name, record it.  Either
                    # this person has played no games or we don't have their
                    # playerId-playerName mapped yet.
                    rating = isotropish.new_player_rating()
                    rating = (rating.mu, rating.sigma, 0)
                rating = {
                    'playerId': pid,
                    'mu': rating[0],
                    'sigma': rating[1],
                    'numgames': rating[2]
                }
                out.append(rating)
            self.interface.respondToClient(client, msgtype, msgid, ratings=out)

        elif msgtype == 'QUERY_ISOLEVEL':
            rating = db_manager.get_rating_by_id(message['playerId'])
            if rating is None:
                db_manager.record_player_id(message['playerId'],
                                            message['playerName'])
                rating = db_manager.get_rating(message['playerName'])
                logging.info('Recording new playerInfo: (%s, %s)' %
                             (message['playerId'], message['playerName']))
            if rating is None:
                isolevel = 0
            else:
                (mu, sigma, numgames) = rating
                isolevel = math.floor(mu - 3 * sigma)
            self.interface.respondToClient(client, msgtype, msgid,
                                           isolevel=isolevel)

        elif msgtype == 'QUERY_ISO_TABLE':
            self.interface.respondToClient(client, msgtype, msgid,
                                           isolevel=self.iso_table)

        elif msgtype == 'QUERY_AVATAR_TABLE':
            if self.avatar_table is None:
                self.avatar_table = db_manager.get_all_avatar_info()
            self.interface.respondToClient(client, msgtype, msgid,
                                           available=self.avatar_table)

        elif msgtype == 'QUERY_AVATAR':
            pid = message['playerId']
            if self.avatar_table is None:
                self.avatar_table = db_manager.get_all_avatar_info()
            if not pid in self.avatar_table:
                logging.info('Avatar info not found.  Looking up on '
                             + 'retrobox -- playerId: %s' % pid)
                url = "http://dom.retrobox.eu/avatars/%s.png"
                url = url % message['playerId']
                r = requests.get(url, stream=True)
                available = r.status_code != 404
                if available:
                    logging.info('Writing avatar to file: %s' % pid)

                    # As uploaded
                    with open(pid, 'wb') as f:
                        for chunk in r.iter_content(1024):
                            f.write(chunk)
                        f.flush()
                        f.close()

                    # As uploaded
                    img = Image.open(pid)
                    (w, h) = img.size
                    img = img.resize((100, 100), Image.ANTIALIAS)
                    img = img.convert('RGB')
                    img.save('web/static/avatars/' + pid + '.jpg', "JPEG",
                             quality=95)

                    # As uploaded
                    try:
                        os.remove(pid)
                    except OSError as e:
                        logging.error("Error: %s - %s."
                                      % (e.filename, e.strerror))

                    db_manager.save_avatar_info(pid, True)
                else:
                    db_manager.save_avatar_info(pid, False)
                self.interface.respondToClient(client, msgtype, msgid,
                                               available=available)
            else:
                logging.debug('Avatar info found for %s %s - %s'
                              % (pid, msgid, self.avatar_table[pid]))
                self.interface.respondToClient(
                    client, msgtype, msgid, playerid=pid,
                    available=self.avatar_table[pid])

        elif msgtype == 'QUERY_ASSESSMENT':
            if message['system'] == 'pro':
                r_me = Rating(message['myRating']['mu'],
                              message['myRating']['sigma'])
                r_opp = Rating(message['hisRating']['mu'],
                               message['hisRating']['sigma'])
                wld_delta = {
                    'win': {'score': 1, 'me': {}, 'opp': {}},
                    'draw': {'score': 0, 'me': {}, 'opp': {}},
                    'loss': {'score': -1, 'me': {}, 'opp': {}}
                }
                for key in wld_delta:
                    (x, y) = rate(r_me, r_opp, wld_delta[key]['score'],
                                  goko_env)
                    wld_delta[key]['me']['mu'] = x.mu - r_me.mu
                    wld_delta[key]['me']['sigma'] = x.sigma - r_me.sigma
                    wld_delta[key]['me']['displayed'] = \
                        (x.mu - 2 * x.sigma) - (r_me.mu - 2 * r_me.sigma)
                self.interface.respondToClient(client, msgtype, msgid,
                                               wld_delta=wld_delta)
            else:
                self.interface.respondToClient(client, msgtype, msgid,
                                               error='Unknown rating system')

        else:
            logging.warn("""Received unknown message type %s from client %s
                         """ % (msgtype, client))
            self.interface.respondToClient(client, msgtype, msgid,
                                           response="Unknown message type")

    def check_iso_levels(self):
        (iso_new, self.iso_latest) = \
            db_manager.get_new_ratings(self.iso_latest)
        for playerId in iso_new:
            self.iso_table[playerId] = iso_new[playerId]
        if iso_new != {}:
            self.interface.sendToAllClients('UPDATE_ISO_LEVELS',
                                            new_levels=iso_new)
