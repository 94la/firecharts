import asyncio
import tempfile
import unittest
from mtanalog.core import Book, SequenceGap
from mtanalog.depth_refresh import DepthRefresh
from mtanalog.collector import RestError


def initial():return dict(lastUpdateId=100,bids=[['100','2'],['90','3']],asks=[['110','4'],['120','5']])
def shifted(identifier=101):return dict(lastUpdateId=identifier,bids=[['95','3'],['85','2']],asks=[['105','1'],['115','1']])


class RefreshBookTests(unittest.TestCase):
    def test_complete_band_moves_retains_far_levels_and_replay_delta_is_exact(self):
        book=Book();book.snapshot(initial())
        event=dict(U=101,u=101,b=[['80','10']],a=[['150','5']]);book.apply(event)
        before=book.export()
        candidate=book.refreshed(shifted(),[])
        self.assertEqual((candidate.low,candidate.high),(85,115))
        self.assertIn(8000,candidate.bids);self.assertIn(15000,candidate.asks)
        self.assertNotIn(9000,candidate.bids) # absent from fresh complete range
        self.assertEqual(book.export(),before)
        record=book.refresh_record(candidate);book.apply_refresh(record)
        self.assertEqual(book.bids,candidate.bids);self.assertEqual(book.asks,candidate.asks);self.assertEqual(book.update_id,candidate.update_id)
        self.assertEqual((book.low,book.high),(85,115))
        self.assertEqual(book.refresh_record(candidate)['b'],[])
        restored=Book();restored.snapshot({**book.export(),'known_low':book.low,'known_high':book.high})
        self.assertEqual((restored.low,restored.high),(85,115))

    def test_missing_buffer_is_rejected_without_mutation(self):
        book=Book();book.snapshot(initial());book.apply(dict(U=101,u=102,b=[['95','7']],a=[]))
        before=book.export()
        with self.assertRaises(SequenceGap):book.refreshed(shifted(100),[dict(U=102,u=102,b=[],a=[])])
        self.assertEqual(book.export(),before)
        candidate=book.refreshed(shifted(101),[dict(U=102,u=102,b=[['95','7']],a=[])])
        self.assertEqual(candidate.update_id,102);self.assertEqual(candidate.bids[9500],7)


class RefreshSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def settle(self,refresh):
        for _ in range(200):
            if refresh.task.done():return
            await asyncio.sleep(.001)
        self.fail('Mock REST did not finish')

    async def test_waits_for_stream_frontier_without_stalling_or_duplicate_fetch(self):
        calls=[]
        def fetch():calls.append(1);return shifted(102)
        book=Book();book.snapshot(initial());book.apply(dict(U=101,u=101,b=[],a=[]))
        refresh=DepthRefresh(fetch,interval=60,stamp=lambda:12345)
        self.assertIsNone(refresh.poll(book));await self.settle(refresh)
        self.assertIsNone(refresh.poll(book));self.assertEqual(book.update_id,101)
        event=dict(U=102,u=102,b=[['95','8']],a=[])
        book.apply(event);refresh.remember(event)
        candidate=refresh.poll(book)
        self.assertIsNotNone(candidate);self.assertEqual(candidate.update_id,102)
        # Snapshot itself already includes update102; replay must not double-apply it.
        self.assertEqual(candidate.bids[9500],3)
        self.assertEqual(refresh.last_success_ms,12345);self.assertEqual(refresh.count,1)
        self.assertIsNone(refresh.poll(candidate));self.assertEqual(len(calls),1)
        await refresh.close()

    async def test_rate_limit_keeps_valid_book_and_respects_retry_after(self):
        def fetch():raise RestError('Binance HTTP 429',retry_after=60)
        book=Book();book.snapshot(initial());before=book.export()
        refresh=DepthRefresh(fetch,interval=15)
        refresh.poll(book);await self.settle(refresh)
        started=asyncio.get_running_loop().time();self.assertIsNone(refresh.poll(book))
        self.assertGreaterEqual(refresh.next_at,started+59)
        self.assertEqual(book.export(),before);self.assertIsNotNone(refresh.error)
        await refresh.close()
