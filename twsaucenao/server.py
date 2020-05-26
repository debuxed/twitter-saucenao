import asyncio
import hashlib
import logging
import reprlib
from typing import *

import tweepy
from pysaucenao import GenericSource, SauceNao, ShortLimitReachedException, SauceNaoException, VideoSource

from twsaucenao.api import twitter_api
from twsaucenao.config import config
from twsaucenao.errors import *


class TwitterSauce:
    def __init__(self):
        self.log = logging.getLogger(__name__)
        self.api = twitter_api()
        self.sauce = SauceNao()

        # Image URL's are md5 hashed and cached here to prevent duplicate API queries. This is cleared every 24-hours.
        # I'll update this in the future to use a real caching mechanism (database or redis)
        self._cached_results = {}

        # The ID cutoff, we populate this once via an initial query at startup
        self.since_id = max([t.id for t in [*tweepy.Cursor(self.api.mentions_timeline).items()]]) or 1
        self.monitored_since = {}

    # noinspection PyBroadException
    async def check_mentions(self) -> None:
        """
        Check for any new mentions we need to parse
        Returns:
            None
        """
        self.log.info(f"Retrieving mentions since tweet {self.since_id}")

        mentions = [*tweepy.Cursor(self.api.mentions_timeline, since_id=self.since_id).items()]  # type: List[tweepy.models.Status]

        # Filter tweets without a reply AND attachment
        for tweet in mentions:
            try:
                # Update the ID cutoff before attempting to parse the tweet
                self.since_id = max([self.since_id, tweet.id])
                media = self.parse_tweet_media(tweet)
                sauce = await self.get_sauce(media[0])
                self.send_reply(tweet, sauce)
            except TwSauceNoMediaException:
                self.log.info(f"Skipping tweet {tweet.id}")
                continue
            except Exception:
                self.log.exception(f"An unknown error occurred while processing tweet {tweet.id}")
                continue

    async def check_monitored(self) -> None:
        """
        Checks monitored accounts for any new tweets
        Returns:
            None
        """
        monitored_accounts = str(config.get('Twitter', 'monitored_accounts'))
        if not monitored_accounts:
            return

        monitored_accounts = [a.strip() for a in monitored_accounts.split(',')]

        for account in monitored_accounts:
            # Have we fetched a tweet for this account yet?
            if account not in self.monitored_since:
                # If not, get the last tweet ID from this account and wait for the next post
                tweet = next(tweepy.Cursor(self.api.user_timeline, account, page=1).items())
                self.monitored_since[account] = tweet.id
                self.log.info(f"Monitoring tweets after {tweet.id} for account {account}")
                return

            # Get all tweets since our last check
            self.log.info(f"[{account}] Retrieving tweets since {self.monitored_since[account]}")
            tweets = [*tweepy.Cursor(self.api.user_timeline, account, since_id=self.monitored_since[account]).items()]  # type: List[tweepy.models.Status]
            self.log.info(f"[{account}] {len(tweets)} tweets found")
            for tweet in tweets:
                try:
                    # Update the ID cutoff before attempting to parse the tweet
                    self.monitored_since[account] = max([self.monitored_since[account], tweet.id])

                    media = self.parse_tweet_media(tweet)
                    self.log.info(f"[{account}] Found new media post in tweet {tweet.id}: {media[0]['media_url_https']}")

                    sauce = await self.get_sauce(media[0])
                    self.log.info(f"[{account}] Found {sauce.index} sauce for tweet {tweet.id}" if sauce
                                  else f"[{account}] Failed to find sauce for tweet {tweet.id}")

                    self.send_reply(tweet, sauce, False)
                except TwSauceNoMediaException:
                    self.log.info(f"[{account}] No sauce found for tweet {tweet.id}")
                    continue
                except Exception:
                    self.log.exception(f"[{account}] An unknown error occurred while processing tweet {tweet.id}")
                    continue

    async def get_sauce(self, media: dict) -> Optional[GenericSource]:
        """
        Get the sauce of a media tweet
        """
        # Have we cached this tweet already?
        url_hash = hashlib.md5(media['media_url_https'].encode()).hexdigest()
        if url_hash in self._cached_results:
            return self._cached_results[url_hash]

        # Look up the sauce
        try:
            sauce = await self.sauce.from_url(media['media_url_https'])
            if not sauce.results:
                self._cached_results[url_hash] = None
                return None
        except ShortLimitReachedException:
            self.log.warning("Short API limit reached, throttling for 30 seconds")
            await asyncio.sleep(30.0)
            return await self.get_sauce(media)
        except SauceNaoException as e:
            self.log.error(f"SauceNao exception raised: {e}")
            return None

        self._cached_results[url_hash] = sauce[0]
        return sauce[0]

    def send_reply(self, tweet: tweepy.models.Status, sauce: Optional[GenericSource], requested=True) -> None:
        """
        Return the source of the image
        Args:
            tweet (tweepy.models.Status): The tweet to reply to
            sauce (Optional[GenericSource]): The sauce found (or None if nothing was found)
            requested (bool): True if the lookup was requested, or False if this is a monitored user account

        Returns:
            None
        """
        if sauce is None:
            if requested:
                self.api.update_status(
                        f"@{tweet.author.screen_name} Sorry, I couldn't find anything for you 😔",
                        in_reply_to_status_id=tweet.id
                )
            return

        # For limiting the length of the title/author
        repr = reprlib.Repr()
        repr.maxstring = 32

        title = repr.repr(sauce.title).strip("'")
        if requested:
            reply = f"@{tweet.author.screen_name} I found something for you on {sauce.index}!\n\nTitle: {title}"
        else:
            reply = f"I found the source of this on {sauce.index}!\n\nTitle: {title}"

        if sauce.author_name:
            author = repr.repr(sauce.author_name).strip("'")
            reply += f"\nAuthor: {author}"

        if isinstance(sauce, VideoSource):
            if sauce.episode:
                reply += f"\nEpisode: {sauce.episode}"
            if sauce.timestamp:
                reply += f"\nTimestamp: {sauce.timestamp}"

        reply += f"\n{sauce.source_url}"

        if not requested:
            reply += f"\n\nI can help you look up the sauce to images elsewhere too! Just mention me in a reply to an image you want to look up."
        self.api.update_status(reply, in_reply_to_status_id=tweet.id, auto_populate_reply_metadata=not requested)

    # noinspection PyUnresolvedReferences
    def parse_tweet_media(self, tweet: tweepy.models.Status) -> List[dict]:
        """
        Determine whether this is a direct tweet or a reply, then parse the media accordingly
        """
        if tweet.in_reply_to_status_id:
            return self._parse_reply(tweet)

        return self._parse_direct(tweet)

    def _parse_direct(self, tweet: tweepy.models.Status) -> List[dict]:
        """
        Direct tweet (someone tweeting at us directly, not as a reply to another tweet)
        Should have media attached, otherwise it's invalid and we ignore it
        """
        try:
            media = tweet.extended_entities['media']  # type: List[dict]
        except AttributeError:
            try:
                media = tweet.entities['media']  # type: List[dict]
            except KeyError:
                self.log.warning(f"Tweet {tweet.id} does not have any downloadable media")
                raise TwSauceNoMediaException

        return media

    def _parse_reply(self, tweet: tweepy.models.Status) -> List[dict]:
        """
        If we were mentioned in a reply, we want to get the sauce to the message we replied to
        """
        try:
            self.log.info(f"Looking up tweet ID {tweet.in_reply_to_status_id}")
            parent = self.api.get_status(tweet.in_reply_to_status_id)
        except tweepy.TweepError:
            self.log.warning(f"Tweet {tweet.in_reply_to_status_id} no longer exists or we don't have permission to view it")
            raise TwSauceNoMediaException

        # No we have a direct tweet to parse!
        return self._parse_direct(parent)
