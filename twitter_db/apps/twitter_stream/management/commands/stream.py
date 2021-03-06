import logging
from optparse import make_option
from logging.config import dictConfig
import time
import Queue

from django.core.exceptions import ObjectDoesNotExist, ImproperlyConfigured
from django.core.management.base import BaseCommand
import signal
import tweepy
from twitter_monitor import JsonStreamListener, DynamicTwitterStream, TermChecker

from ...models import TwitterAPICredentials, FilterTerm, Tweet, StreamProcess


# Setup logging if not already configured
logger = logging.getLogger('twitter_stream')
if not logger.handlers:
    dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {
            "twitter_stream": {
                "level": "DEBUG",
                "class": "logging.StreamHandler",
            },
        },
        "twitter_stream": {
            "handlers": ["twitter_stream"],
            "level": "DEBUG"
        }
    })


class TweetQueue(Queue.Queue):
    def get_all(self, block=True, timeout=None):
        """Remove and return all the items from the queue.

        If optional args 'block' is true and 'timeout' is None (the default),
        block if necessary until an item is available. If 'timeout' is
        a non-negative number, it blocks at most 'timeout' seconds and raises
        the Empty exception if no item was available within that time.
        Otherwise ('block' is false), return an item if one is immediately
        available, else raise the Empty exception ('timeout' is ignored
        in that case).
        """
        self.not_empty.acquire()
        try:
            if not block:
                if not self._qsize():
                    raise Queue.Empty
            elif timeout is None:
                while not self._qsize():
                    self.not_empty.wait()
            elif timeout < 0:
                raise ValueError("'timeout' must be a non-negative number")
            else:
                endtime = time() + timeout
                while not self._qsize():
                    remaining = endtime - time()
                    if remaining <= 0.0:
                        raise Queue.Empty
                    self.not_empty.wait(remaining)
            items = self._get_all()
            self.not_full.notify()
            return items
        finally:
            self.not_empty.release()

    def get_all_nowait(self):
        """Remove and return all the items from the queue without blocking.

        Only get items if immediately available. Otherwise
        raise the Empty exception.
        """
        return self.get_all(False)

    def _get_all(self):
        """
        Get all the items from the queue.
        """
        result = []
        while len(self.queue):
            result.append(self.queue.popleft())
        return result


class FeelsTermChecker(TermChecker):
    """
    Checks the database for filter terms.

    Note that because this is run every now and then, and
    so as not to block the streaming thread, this
    object will actually also insert the tweets into the database.
    """

    def __init__(self, queue_listener, stream_process):
        super(FeelsTermChecker, self).__init__()

        # A queue for tweets that need to be written to the database
        self.listener = queue_listener
        self.error_count = 0
        self.process = stream_process

    def update_tracking_terms(self):
        # Process the tweet queue -- this is more important
        # to do regularly than updating the tracking terms
        # Update the process status in the database
        self.process.tweet_rate = self.listener.process_tweet_queue()
        self.process.error_count = self.error_count

        # Check for new tracking terms
        filter_terms = FilterTerm.objects.filter(enabled=True)

        if len(filter_terms):
            self.process.status = StreamProcess.STREAM_STATUS_RUNNING
        else:
            self.process.status = StreamProcess.STREAM_STATUS_WAITING

        self.process.heartbeat()

        return set([t.term for t in filter_terms])

    def ok(self):
        return self.error_count < 5

    def error(self, exc):
        logger.error(exc)
        self.error_count += 1


class QueueStreamListener(JsonStreamListener):
    """
    Saves tweets in a queue for later insertion into database
    when process_tweet_batch() is called.

    Note that this is operated by the streaming thread.
    """

    def __init__(self, api=None):
        super(QueueStreamListener, self).__init__(api)

        # A place to put the tweets
        self.queue = TweetQueue()

        # For calculating tweets / sec
        self.time = time.time()

    def on_status(self, status):
        # construct a Tweet object from the raw status object.
        self.queue.put_nowait(status)

    def on_disconnect(self, code, stream_name, reason):
        logger.warning("Received disconnect! code %d on stream %s: %s", code, stream_name, reason)
        return True

    def process_tweet_queue(self):
        """
        Inserts any queued tweets into the database.

        It is ok for this to be called on a thread other than the streaming thread.
        """

        # this is for calculating the tps rate
        now = time.time()
        diff = now - self.time
        self.time = now

        try:
            batch = self.queue.get_all_nowait()
        except Queue.Empty:
            return 0

        if len(batch) == 0:
            return 0

        # we will ignore the possible embedded original tweet (e.g. this is a retweet)
        tweets = [Tweet.create_from_json(status) for status in batch]
        Tweet.objects.bulk_create(tweets)

        logger.info("Inserted %s tweets at %s tps" % (len(batch), len(batch) / diff))
        return len(batch) / diff


def get_credentials(credentials_name):
    if credentials_name:
        credentials = TwitterAPICredentials.objects.get(name=credentials_name)
    else:
        credentials = TwitterAPICredentials.objects.first()

    if not credentials:
        raise ObjectDoesNotExist("Unknown credentials %s" % credentials_name)

    return credentials


class Command(BaseCommand):
    """
    Starts a process that streams data from Twitter.

    Example usage:
    python manage.py stream
    python manage.py stream --poll-interval 25
    """

    option_list = BaseCommand.option_list + (
        make_option(
            '--poll-interval',
            action='store',
            dest='poll_interval',
            default=10,
            help='Seconds between term updates and tweet inserts.'
        ),
    )
    args = '<credentials_name>'
    help = "Starts a streaming connection to Twitter"

    def handle(self, credentials_name=None, *args, **options):

        # The suggested time between hearbeats
        poll_interval = options.get('poll_interval', 10)

        # First expire any old stream process records that have failed
        # to report in for a while
        timeout_seconds = 3 * poll_interval
        StreamProcess.expire_timed_out()

        stream_process = StreamProcess.create(
            timeout_seconds=3 * poll_interval
        )


        def stop(signum, frame):
            """
            Handle sudden stops
            """
            logger.debug("Stopping because of signal")

            if stream_process:
                stream_process.status = StreamProcess.STREAM_STATUS_STOPPED
                stream_process.heartbeat()

            raise SystemExit()

        def install_signal_handlers():
            """
            Installs signal handlers for handling SIGINT and SIGTERM
            gracefully.
            """

            signal.signal(signal.SIGINT, stop)
            signal.signal(signal.SIGTERM, stop)

        install_signal_handlers()

        try:
            credentials = get_credentials(credentials_name)

            logger.info("Using credentials for %s", credentials.name)
            stream_process.credentials = credentials
            stream_process.save()

            auth = tweepy.OAuthHandler(credentials.api_key, credentials.api_secret)
            auth.set_access_token(credentials.access_token, credentials.access_token_secret)

            listener = QueueStreamListener()
            checker = FeelsTermChecker(queue_listener=listener,
                                       stream_process=stream_process)

            # Start and maintain the streaming connection...
            stream = DynamicTwitterStream(auth, listener, checker)

            while checker.ok():
                try:
                    stream.start(poll_interval)
                except Exception as e:
                    checker.error(e)
                    time.sleep(3)  # to avoid craziness

            logger.error("Stopping because term checker not ok")
            stream_process.status = StreamProcess.STREAM_STATUS_STOPPED
            stream_process.heartbeat()

        except Exception as e:
            logger.error(e)

        finally:
            stop(None, None)
