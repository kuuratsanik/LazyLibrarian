import lazylibrarian
from lazylibrarian import logger
from lazylibrarian.common import notifyStrings, NOTIFY_SNATCH, NOTIFY_DOWNLOAD
from lib.pynma import pynma


class NMA_Notifier:

    @staticmethod
    def _sendNMA(nma_api=None, nma_priority=None, event=None, message=None, force=False):

        title = "LazyLibrarian"

        # suppress notifications if the notifier is disabled but the notify options are checked
        if not lazylibrarian.CONFIG['USE_NMA'] and not force:
            return False

        if nma_api is None:
            nma_api = lazylibrarian.CONFIG['NMA_APIKEY']

        if nma_priority is None:
            nma_priority = lazylibrarian.CONFIG['NMA_PRIORITY']

        logger.debug(u"NMA: title: " + title)
        logger.debug(u"NMA: event: " + event)
        logger.debug(u"NMA: message: " + message)

        batch = False

        p = pynma.PyNMA()
        keys = nma_api.split(',')
        p.addkey(keys)

        if len(keys) > 1:
            batch = True

        response = p.push(title, event, message, priority=nma_priority, batch_mode=batch)

        if not response[nma_api][u'code'] == u'200':
            logger.error(u"NMA: Could not send notification to NotifyMyAndroid")
            return False
        else:
            logger.debug(u"NMA: Success. NotifyMyAndroid returned : %s" % response[nma_api][u'code'])
            return True

#
# Public functions
#

    def notify_snatch(self, title):
        if lazylibrarian.CONFIG['NMA_ONSNATCH']:
            self._sendNMA(nma_api=None, nma_priority=None, event=notifyStrings[NOTIFY_SNATCH], message=title)

    def notify_download(self, title):
        if lazylibrarian.CONFIG['NMA_ONDOWNLOAD']:
            self._sendNMA(nma_api=None, nma_priority=None, event=notifyStrings[NOTIFY_DOWNLOAD], message=title)

    # noinspection PyUnusedLocal
    def test_notify(self, title="Test"):
        return self._sendNMA(nma_api=None, nma_priority=None, event="Test", message="Testing NMA settings from LazyLibrarian", force=True)

    def update_library(self, showName=None):
        pass

notifier = NMA_Notifier
