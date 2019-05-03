import pprint
import uuid
import aioboto3
import uuid
import asyncio
import logging, coloredlogs
from asynctest import TestCase, fail_on

from kinesis import Consumer, Producer, MemoryCheckPointer, RedisCheckPointer
from kinesis import exceptions

coloredlogs.install(level='DEBUG')

logging.getLogger('botocore').setLevel(logging.WARNING)

log = logging.getLogger(__name__)

ENDPOINT_URL = 'http://localhost:4567'


class BaseKinesisTests(TestCase):

    async def setUp(self):
        self.stream_name = "test_{}".format(str(uuid.uuid4())[0:8])

        # Uses global for storing session. wipe it out otherwise will be using this loop UGH
        aioboto3.DEFAULT_SESSION = None

    def random_string(self, length):
        from random import choice
        from string import ascii_uppercase

        return ''.join(choice(ascii_uppercase) for i in range(length))

    async def add_record_delayed(self, msg, producer, delay):
        log.debug("Adding record. delay={}".format(delay))
        await asyncio.sleep(delay)
        await producer.put(msg)


class CheckpointTests(BaseKinesisTests):
    """
    Checkpoint Tests
    """

    @classmethod
    def patch_consumer_fetch(cls, consumer):
        async def get_shard_iterator(shard_id, last_sequence_number=None):
            log.info("getting shard iterator for {} @ {}".format(shard_id, last_sequence_number))
            return True

        consumer.get_shard_iterator = get_shard_iterator

        async def get_records(shard):
            log.info("get records shard={}".format(shard['ShardId']))
            return {}

        consumer.get_records = get_records

        consumer.is_fetching = True

    async def test_memory_checkpoint(self):
        # first consumer
        checkpointer = MemoryCheckPointer(name="test")

        consumer_a = Consumer(stream_name=None, checkpointer=checkpointer, max_shard_consumers=1)

        self.patch_consumer_fetch(consumer_a)

        consumer_a.shards = [{'ShardId': 'test-1'}, {'ShardId': 'test-2'}]

        await consumer_a.fetch()

        shards = [s['ShardId'] for s in consumer_a.shards if s.get('stats')]

        # Expect only one shard assigned as max = 1
        self.assertEqual(['test-1'], shards)

        # second consumer

        consumer_b = Consumer(stream_name=None, checkpointer=checkpointer, max_shard_consumers=1)

        self.patch_consumer_fetch(consumer_b)

        consumer_b.shards = [{'ShardId': 'test-1'}, {'ShardId': 'test-2'}]

        await consumer_b.fetch()

        shards = [s['ShardId'] for s in consumer_b.shards if s.get('stats')]

        # Expect only one shard assigned as max = 1
        self.assertEqual(['test-2'], shards)

    async def test_redis_checkpoint_locking(self):
        name = "test-{}".format(str(uuid.uuid4())[0:8])

        # first consumer
        checkpointer_a = RedisCheckPointer(name=name, id='proc-1')

        # second consumer
        checkpointer_b = RedisCheckPointer(name=name, id='proc-2')

        # try to allocate the same shard

        result = await asyncio.gather(
            *[
                checkpointer_a.allocate('test'),
                checkpointer_b.allocate('test')
            ]
        )

        result = list(sorted([x[0] for x in result]))

        # Expect only one to have succeeded
        self.assertEquals([False, True], result)

        await checkpointer_a.close()
        await checkpointer_b.close()

    async def test_redis_checkpoint_reallocate(self):
        name = "test-{}".format(str(uuid.uuid4())[0:8])

        # first consumer
        checkpointer_a = RedisCheckPointer(name=name, id='proc-1')

        await checkpointer_a.allocate('test')

        # checkpoint
        await checkpointer_a.checkpoint('test', '123')

        # stop on this shard
        await checkpointer_a.deallocate('test')

        # second consumer
        checkpointer_b = RedisCheckPointer(name=name, id='proc-2')

        success, sequence = await checkpointer_b.allocate('test')

        self.assertTrue(success)
        self.assertEquals("123", sequence)

        await checkpointer_b.close()

        self.assertEquals(await checkpointer_b.get_all_checkpoints(), {})

        await checkpointer_a.close()


    async def test_redis_checkpoint_hearbeat(self):
        name = "test-{}".format(str(uuid.uuid4())[0:8])

        checkpointer = RedisCheckPointer(name=name, heartbeat_frequency=0.5)

        await checkpointer.allocate('test')
        await checkpointer.checkpoint('test', '123')

        await asyncio.sleep(1)

        await checkpointer.close()

        # nothing to assert
        self.assertTrue(True)


class KinesisTests(BaseKinesisTests):
    """
    Kinesa Lite Tests
    """

    async def test_stream_does_not_exist(self):
        with self.assertRaises(exceptions.StreamDoesNotExist):
            async with Producer(stream_name=self.stream_name, endpoint_url=ENDPOINT_URL) as producer:
                await producer.put('test')

    async def test_create_stream_shard_limit_exceeded(self):
        with self.assertRaises(exceptions.StreamShardLimit):
            async with Producer(stream_name=self.stream_name, endpoint_url=ENDPOINT_URL) as producer:
                await producer.create_stream(shards=1000)

    @fail_on(unused_loop=True, active_handles=True)
    async def test_producer_put(self):
        async with Producer(stream_name=self.stream_name, endpoint_url=ENDPOINT_URL) as producer:
            await producer.create_stream(shards=1)
            await producer.put('test')

    async def test_producer_put_below_limit(self):
        async with Producer(stream_name=self.stream_name, endpoint_url=ENDPOINT_URL) as producer:
            await producer.create_stream(shards=1)
            # The maximum size of the data payload of a record before base64-encoding is up to 1 MiB.
            await producer.put(self.random_string(1024 * 1023))

    async def test_producer_put_above_limit(self):
        with self.assertRaises(exceptions.ExceededPutLimit):
            async with Producer(stream_name=self.stream_name, endpoint_url=ENDPOINT_URL) as producer:
                await producer.create_stream(shards=1)
                # The maximum size of the data payload of a record before base64-encoding is up to 1 MiB.
                await producer.put(self.random_string(1024 * 1024))


    async def test_producer_put_with_batching(self):
        # todo
        pass


    async def test_producer_put_above_limit_with_msgpack(self):
        # todo
        pass

    async def test_producer_put(self):
        # Expect to complete by lowering batch size until successful
        async with Producer(stream_name=self.stream_name, endpoint_url=ENDPOINT_URL,
                            batch_size=600
                            ) as producer:
            await producer.create_stream(shards=1)

            for x in range(1000):
                await producer.put('test')

    async def test_producer_and_consumer(self):

        async with Producer(stream_name=self.stream_name, endpoint_url=ENDPOINT_URL) as producer:
            await producer.create_stream(shards=1)

            async with Consumer(stream_name=self.stream_name,
                                endpoint_url=ENDPOINT_URL,
                                ):
                pass

    async def test_producer_and_consumer_consume(self):
        async with Producer(stream_name=self.stream_name, endpoint_url=ENDPOINT_URL) as producer:
            await producer.create_stream(shards=1)

            await producer.put("test")

            await producer.flush()

            results = []

            async with Consumer(stream_name=self.stream_name,
                                endpoint_url=ENDPOINT_URL,
                                ) as consumer:
                async for item in consumer:
                    results.append(item)

            # Expect to have consumed from start as default iterator_type=TRIM_HORIZON
            self.assertEquals(["test"], results)

    async def test_producer_and_consumer_consume_queue_full(self):
        async with Producer(stream_name=self.stream_name, endpoint_url=ENDPOINT_URL) as producer:
            await producer.create_stream(shards=1)

            for i in range(0, 100):
                await producer.put("test")

            await producer.flush()

            results = []

            async with Consumer(stream_name=self.stream_name,
                                endpoint_url=ENDPOINT_URL,
                                max_queue_size=20
                                ) as consumer:

                async for item in consumer:
                    results.append(item)

            # Expect 20 only as queue is full and we don't wait on queue
            self.assertEqual(20, len(results))

    async def test_producer_and_consumer_consume_throttle(self):
        async with Producer(stream_name=self.stream_name, endpoint_url=ENDPOINT_URL) as producer:
            await producer.create_stream(shards=1)

            for i in range(0, 100):
                await producer.put("test")

            await producer.flush()

            results = []

            async with Consumer(stream_name=self.stream_name,
                                endpoint_url=ENDPOINT_URL,
                                record_limit=10,
                                # 2 per second
                                shard_fetch_rate=2
                                ) as consumer:

                from datetime import datetime
                dt = datetime.now()

                while (datetime.now() - dt).total_seconds() < 3.05:
                    async for item in consumer:
                        results.append(item)

            # Expect 2*3*10 = 60  ie at most 6 iterations of 10 records
            self.assertGreaterEqual(len(results), 50)
            self.assertLessEqual(len(results), 70)

    async def test_producer_and_consumer_consume_with_checkpointer_and_latest(self):
        async with Producer(stream_name=self.stream_name, endpoint_url=ENDPOINT_URL) as producer:
            await producer.create_stream(shards=1)

            await producer.put("test.A")

            results = []

            checkpointer = MemoryCheckPointer(name="test")

            async with Consumer(stream_name=self.stream_name,
                                endpoint_url=ENDPOINT_URL,
                                checkpointer=checkpointer,
                                iterator_type="LATEST",
                                ) as consumer:

                async for item in consumer:
                    results.append(item)

            # Expect none as LATEST
            self.assertEquals([], results)

            checkpoints = checkpointer.get_all_checkpoints()

            # Expect 1 as only 1 shard
            self.assertEquals(1, len(checkpoints))

            # none as no records yet (using LATEST)
            self.assertIsNone(checkpoints[list(checkpoints.keys())[0]])

            results = []

            log.info("Starting consumer again..")

            checkpointer = MemoryCheckPointer(name="test")

            async with Consumer(stream_name=self.stream_name,
                                endpoint_url=ENDPOINT_URL,
                                checkpointer=checkpointer,
                                iterator_type="LATEST",
                                sleep_time_no_records=0.5
                                ) as consumer:

                # Manually start (so we can be sure we got some results)
                await consumer.start_consumer()

                await producer.put("test.B")

                await producer.flush()

                log.info('waiting before consuming..')

                await asyncio.sleep(1)

                log.info('about to consume..')

                async for item in consumer:
                    results.append(item)

            self.assertEquals(['test.B'], results)

            checkpoints = checkpointer.get_all_checkpoints()

            # expect not None as has processed records
            self.assertIsNotNone(checkpoints[list(checkpoints.keys())[0]])

            # now add some records
            for i in range(0, 10):
                await producer.put("test.{}".format(i))

            await producer.flush()

            await asyncio.sleep(1)

            results = []

            async with Consumer(stream_name=self.stream_name,
                                endpoint_url=ENDPOINT_URL,
                                checkpointer=checkpointer,
                                iterator_type="LATEST",
                                sleep_time_no_records=0.5
                                ) as consumer:

                async for item in consumer:
                    results.append(item)

            self.assertEquals(10, len(results))

    async def test_producer_and_consumer_consume_multiple_shards_with_redis_checkpointer(self):
        async with Producer(stream_name=self.stream_name, endpoint_url=ENDPOINT_URL) as producer:
            await producer.create_stream(shards=2)

            for i in range(0, 100):
                await producer.put("test.{}".format(i))

            await producer.flush()

            results = []

            checkpointer = RedisCheckPointer(name="test-{}".format(str(uuid.uuid4())[0:8]), heartbeat_frequency=3)

            #checkpointer = MemoryCheckPointer(name="test")

            async with Consumer(stream_name=self.stream_name,
                                endpoint_url=ENDPOINT_URL,
                                checkpointer=checkpointer,
                                record_limit=10,
                                ) as consumer:

                # consumer will stop if no msgs
                for i in range(0, 6):
                    async for item in consumer:
                        results.append(item)
                    await asyncio.sleep(0.5)

                self.assertEquals(100, len(results))

                checkpoints = checkpointer.get_all_checkpoints()

                self.assertEquals(2, len(checkpoints))

                # Expect both shards to have been used/set
                for item in checkpoints.values():
                    self.assertIsNotNone(item)




class AWSKinesisTests(BaseKinesisTests):
    """
    AWS Kinesis Tests
    """

    STREAM_NAME_SINGLE_SHARD = "pykinesis-test-single-shard"
    STREAM_NAME_MULTI_SHARD = "pykinesis-test-multi-shard"

    forbid_get_event_loop = True

    @classmethod
    def setUpClass(cls):
        log.info("Creating (or ignoring if exists) *Actual* Kinesis stream: {}".format(cls.STREAM_NAME_SINGLE_SHARD))

        async def create(loop, stream_name, shards):
            async with Producer(loop=loop, stream_name=stream_name) as producer:
                await producer.create_stream(shards=shards)
                await producer.start()

        setup_loop = asyncio.new_event_loop()

        asyncio.gather(*[
            create(loop=setup_loop, stream_name=cls.STREAM_NAME_SINGLE_SHARD, shards=1),
            #   create(loop=setup_loop, stream_name=cls.STREAM_NAME_MULTI_SHARD, shards=3)
        ], loop=setup_loop)

        setup_loop.run_until_complete(asyncio.sleep(1, loop=setup_loop))

        setup_loop.close()

    @classmethod
    def tearDownClass(cls):
        log.warning("Don't forget to delete your $$ streams: {} and {}".format(cls.STREAM_NAME_SINGLE_SHARD,
                                                                               cls.STREAM_NAME_MULTI_SHARD))

    async def test_consumer_consume_fetch_limit(self):
        # logging.getLogger('kinesis.consumer').setLevel(logging.WARNING)

        async with Consumer(stream_name=self.STREAM_NAME_SINGLE_SHARD,
                            sleep_time_no_records=0.001,
                            shard_fetch_rate=100,
                            ) as consumer:
            await consumer.start()

            # GetShardIterator has a limit of five transactions per second per account per open shard

            for i in range(0, 100):
                await consumer.fetch()
                # sleep 50ms
                await asyncio.sleep(0.05)

            shard_stats = [s['stats'] for s in consumer.shards][0].to_data()

            self.assertTrue(shard_stats['throttled'] > 0, "Expected to be throttled")
