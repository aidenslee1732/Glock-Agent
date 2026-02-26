"""Metering worker for aggregating usage events."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..storage.postgres import PostgresClient
from ..storage.redis import RedisClient
from .events import UsageEventType, UsageUnit

logger = logging.getLogger(__name__)


@dataclass
class MeteringConfig:
    """Configuration for metering worker."""

    # Processing
    batch_size: int = 100
    flush_interval_seconds: float = 60.0
    stream_key: str = "q:metering"
    consumer_group: str = "metering-workers"
    consumer_name: str = "worker-1"

    # Aggregation
    aggregation_window_hours: int = 1
    retention_days: int = 90

    # Error handling
    max_retries: int = 3
    dead_letter_key: str = "q:metering:deadletter"


class MeteringWorker:
    """Worker that processes usage events and creates rollups.

    The worker:
    1. Consumes events from Redis stream
    2. Batches events for efficient processing
    3. Writes raw events to database
    4. Aggregates into hourly rollups
    5. Handles failures with dead-letter queue
    """

    def __init__(
        self,
        redis: RedisClient,
        db: PostgresClient,
        config: Optional[MeteringConfig] = None
    ):
        self.redis = redis
        self.db = db
        self.config = config or MeteringConfig()

        self._running = False
        self._batch: list[dict[str, Any]] = []
        self._last_flush = datetime.now(timezone.utc)

    async def start(self):
        """Start the metering worker."""
        logger.info("Starting metering worker")
        self._running = True

        # Ensure consumer group exists
        try:
            await self.redis.xgroup_create(
                self.config.stream_key,
                self.config.consumer_group,
                mkstream=True
            )
        except Exception:
            # Group may already exist
            pass

        # Start processing loops
        await asyncio.gather(
            self._process_loop(),
            self._flush_loop(),
            self._aggregation_loop()
        )

    async def stop(self):
        """Stop the metering worker."""
        logger.info("Stopping metering worker")
        self._running = False

        # Flush remaining batch
        if self._batch:
            await self._flush_batch()

    async def _process_loop(self):
        """Main processing loop - consume events from stream."""
        while self._running:
            try:
                # Read batch of events
                events = await self.redis.xreadgroup(
                    self.config.consumer_group,
                    self.config.consumer_name,
                    {self.config.stream_key: ">"},
                    count=self.config.batch_size,
                    block=1000  # 1 second
                )

                if events:
                    for stream_name, messages in events:
                        for msg_id, data in messages:
                            try:
                                await self._process_event(msg_id, data)
                            except Exception as e:
                                logger.error(f"Failed to process event {msg_id}: {e}")
                                await self._handle_error(msg_id, data, e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in process loop: {e}")
                await asyncio.sleep(1)

    async def _flush_loop(self):
        """Periodic flush loop."""
        while self._running:
            try:
                await asyncio.sleep(self.config.flush_interval_seconds)

                if self._batch:
                    await self._flush_batch()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in flush loop: {e}")

    async def _aggregation_loop(self):
        """Periodic aggregation loop - create hourly rollups."""
        while self._running:
            try:
                # Run aggregation at the top of each hour
                now = datetime.now(timezone.utc)
                next_hour = (now + timedelta(hours=1)).replace(
                    minute=0, second=0, microsecond=0
                )
                wait_seconds = (next_hour - now).total_seconds()

                await asyncio.sleep(min(wait_seconds + 60, 3600))  # Max 1 hour

                await self._run_aggregation()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in aggregation loop: {e}")

    async def _process_event(self, msg_id: str, data: dict[str, Any]):
        """Process a single usage event."""
        # Parse event data
        event = self._parse_event(data)

        # Add to batch
        self._batch.append({
            "msg_id": msg_id,
            "event": event
        })

        # Flush if batch is full
        if len(self._batch) >= self.config.batch_size:
            await self._flush_batch()

    def _parse_event(self, data: dict[str, Any]) -> dict[str, Any]:
        """Parse event data from Redis."""
        # Handle bytes from Redis
        parsed = {}
        for key, value in data.items():
            if isinstance(key, bytes):
                key = key.decode()
            if isinstance(value, bytes):
                value = value.decode()
            parsed[key] = value

        # Parse JSON fields
        if "metadata" in parsed and isinstance(parsed["metadata"], str):
            import json
            try:
                parsed["metadata"] = json.loads(parsed["metadata"])
            except json.JSONDecodeError:
                parsed["metadata"] = {}

        # Parse quantity
        if "quantity" in parsed:
            parsed["quantity"] = float(parsed["quantity"])

        return parsed

    async def _flush_batch(self):
        """Flush batch of events to database."""
        if not self._batch:
            return

        batch = self._batch
        self._batch = []
        self._last_flush = datetime.now(timezone.utc)

        try:
            # Insert events into database
            events = [item["event"] for item in batch]
            await self.db.bulk_insert_usage_events(events)

            # Acknowledge messages
            msg_ids = [item["msg_id"] for item in batch]
            await self.redis.xack(
                self.config.stream_key,
                self.config.consumer_group,
                *msg_ids
            )

            logger.info(f"Flushed {len(batch)} usage events")

        except Exception as e:
            logger.error(f"Failed to flush batch: {e}")
            # Put events back for retry
            self._batch = batch + self._batch

    async def _run_aggregation(self):
        """Run hourly aggregation to create rollups."""
        logger.info("Running usage aggregation")

        # Calculate the hour to aggregate (previous hour)
        now = datetime.now(timezone.utc)
        bucket_hour = (now - timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )

        try:
            # Aggregate by user, metric
            await self.db.execute("""
                INSERT INTO usage_rollups_hourly (bucket_hour, user_id, org_id, metric, value, dimensions)
                SELECT
                    date_trunc('hour', created_at) as bucket_hour,
                    user_id,
                    org_id,
                    event_type as metric,
                    SUM(quantity) as value,
                    jsonb_build_object('unit', unit) as dimensions
                FROM usage_events
                WHERE created_at >= $1 AND created_at < $2
                GROUP BY date_trunc('hour', created_at), user_id, org_id, event_type, unit
                ON CONFLICT (bucket_hour, user_id, metric, dimensions_hash)
                DO UPDATE SET value = usage_rollups_hourly.value + EXCLUDED.value
            """, bucket_hour, bucket_hour + timedelta(hours=1))

            logger.info(f"Completed aggregation for {bucket_hour}")

        except Exception as e:
            logger.error(f"Aggregation failed: {e}")

    async def _handle_error(self, msg_id: str, data: dict[str, Any], error: Exception):
        """Handle processing error - send to dead letter queue."""
        try:
            import json

            await self.redis.xadd(
                self.config.dead_letter_key,
                {
                    "original_id": msg_id,
                    "data": json.dumps(data, default=str),
                    "error": str(error),
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            )

            # Acknowledge the original message so we don't retry forever
            await self.redis.xack(
                self.config.stream_key,
                self.config.consumer_group,
                msg_id
            )

        except Exception as e:
            logger.error(f"Failed to handle error for {msg_id}: {e}")

    async def run_cleanup(self):
        """Run periodic cleanup of old data."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.config.retention_days)

        try:
            # Delete old raw events (keep rollups longer)
            deleted = await self.db.delete_old_usage_events(cutoff)
            logger.info(f"Cleaned up {deleted} old usage events")

            # Trim Redis stream
            await self.redis.xtrim(
                self.config.stream_key,
                maxlen=100000,
                approximate=True
            )

        except Exception as e:
            logger.error(f"Cleanup failed: {e}")
