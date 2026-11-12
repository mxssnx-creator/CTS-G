"""Signed pooled transport must preserve bytes and dispatch writes only once."""
import hashlib
import hmac
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import urllib.parse
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server/pulse'))
import bingx_fast as bx


@unittest.skipIf(bx.httpx is None,'httpx runtime dependency unavailable')
class SignedPool(unittest.TestCase):
    def client(self,handler):
        client=bx.FastBingX.__new__(bx.FastBingX)
        client.base='https://open-api-vst.bingx.com';client.key='fixture-key';client.secret='fixture-secret'
        client.http=bx.httpx.Client(base_url=client.base,headers={'X-BX-APIKEY':client.key},
                                   transport=bx.httpx.MockTransport(handler))
        client._opener=SimpleNamespace(open=lambda *args,**kwargs:self.fail('pooled request used urllib'))
        self.addCleanup(client.http.close)
        return client

    def signed_target(self,client):
        values={'timestamp':'1700000000000','recvWindow':'10000',
                'batchOrders':json.dumps([{'quantity':.25,'stopPrice':1.2,'clientOrderID':'spaces + ? & / % ü'}],ensure_ascii=False,separators=(',',':'))}
        return '/openApi/swap/v2/trade/batchOrders?'+client._sign(values)

    def test_query_signature_json_and_verb_are_byte_identical(self):
        seen=[]
        def server(request):
            seen.append(request)
            params=dict(urllib.parse.parse_qsl(request.url.query.decode(),keep_blank_values=True))
            signature=params.pop('signature')
            raw='&'.join(f'{key}={params[key]}'for key in sorted(params))
            self.assertEqual(signature,hmac.new(b'fixture-secret',raw.encode(),hashlib.sha256).hexdigest())
            self.assertEqual(request.headers['X-BX-APIKEY'],'fixture-key')
            return bx.httpx.Response(200,json={'code':0,'data':{'accepted':True}})
        client=self.client(server);target=self.signed_target(client)
        for method in('GET','POST','DELETE'):
            self.assertEqual(client._http(method,target)['code'],0)
            self.assertEqual(seen[-1].method,method)
            self.assertEqual(seen[-1].url.raw_path,target.encode('ascii'))
            self.assertEqual(seen[-1].content,b'')
        self.assertEqual(len(seen),3)

    def test_unknown_write_outcome_never_falls_back_or_resubmits(self):
        calls=[]
        def server(request):
            calls.append(request)
            raise bx.httpx.ReadTimeout('synthetic unknown write',request=request)
        client=self.client(server)
        reply=client._http('POST',self.signed_target(client))
        self.assertTrue(reply['error']);self.assertEqual(reply['code'],-1)
        self.assertEqual(len(calls),1)
        self.assertNotIn('signature',reply['msg'])

    def test_definite_api_refusal_is_returned_without_another_dispatch(self):
        calls=[]
        def server(request):
            calls.append(request)
            return bx.httpx.Response(400,json={'code':101400,'msg':'quantity invalid'})
        client=self.client(server)
        reply=client._http('POST',self.signed_target(client))
        self.assertEqual(reply['code'],101400);self.assertEqual(len(calls),1)

    def test_parameter_merging_mismatch_uses_legacy_transport_before_dispatch(self):
        seen=[]
        client=self.client(lambda request:self.fail('changed signed target reached the network'))
        client.http.params={'unexpected':'extra'}
        def open_legacy(request,**kwargs):
            seen.append(request)
            return io.BytesIO(b'{"code":0,"data":{}}')
        client._opener=SimpleNamespace(open=open_legacy)
        target=self.signed_target(client)
        self.assertEqual(client._http('POST',target)['code'],0)
        self.assertEqual(len(seen),1)
        self.assertEqual(urllib.parse.urlsplit(seen[0].full_url).query,urllib.parse.urlsplit(target).query)

    def test_no_httpx_client_retains_single_legacy_dispatch(self):
        client=self.client(lambda request:self.fail('unused pool'))
        client.http.close();client.http=None;seen=[]
        client._opener=SimpleNamespace(open=lambda request,**kwargs:seen.append(request)or io.BytesIO(b'{"code":0}'))
        self.assertEqual(client._http('DELETE',self.signed_target(client))['code'],0)
        self.assertEqual(len(seen),1)

    def test_private_order_lookup_uses_canonical_normalized_client_id(self):
        requests=[]
        client=self.client(lambda request:requests.append(request)or bx.httpx.Response(200,json={'code':0}))
        client._take=lambda *args:True;client._next_ts=lambda:1700000000000
        client.stats={'rest':0,'err':0};client._trip=lambda *args:None
        client.get('/openApi/swap/v2/trade/order',{'symbol':'A-USDT','clientOrderID':'GtestMIXED'})
        fields=dict(urllib.parse.parse_qsl(requests[0].url.query.decode()))
        self.assertEqual(fields['clientOrderId'],'gtestmixed')
        self.assertNotIn('clientOrderID',fields)

    def test_explicit_order_absence_does_not_cool_the_whole_order_path(self):
        client=self.client(lambda request:self.fail('no request expected'))
        client.stats={'rl':0};client.path_cd={};client.cooldown_until=0
        logs=[];client.err=SimpleNamespace(write=lambda *args,**kwargs:logs.append(kwargs))
        path='/openApi/swap/v2/trade/order'
        client._trip(path,{'code':109421,'msg':'order not exist'})
        self.assertEqual(client.stats['rl'],0);self.assertEqual(client.path_cd,{})
        client._trip(path,{'code':109421,'msg':'rate limit exceeded'})
        self.assertEqual(client.stats['rl'],1)
        self.assertIn(path,client.path_cd)

    def test_commission_queries_reserve_separate_nonbursting_slots(self):
        client=bx.FastBingX('fixture-key','fixture-secret',SimpleNamespace(write=lambda *a,**k:None),base='https://open-api-vst.bingx.com')
        self.addCleanup(client.http.close)
        bucket=client.path_buckets['/openApi/swap/v2/user/commissionRate']
        self.assertEqual((bucket.rate,bucket.burst),(4.0,1.0))
        with patch.object(bx.time,'monotonic',return_value=bucket.ts):
            self.assertEqual([bucket.reserve()for _ in range(5)],[0,.25,.5,.75,1.0])
        seen=[]
        client.buckets['private']=SimpleNamespace(take=lambda:seen.append('private')or 0)
        client.path_buckets['/openApi/swap/v2/user/commissionRate']=SimpleNamespace(take=lambda:seen.append('fee')or .25)
        self.assertTrue(client._take('private','/openApi/swap/v2/user/commissionRate'))
        self.assertEqual(seen,['private','fee'])

    def test_cooldown_started_during_queue_wait_prevents_dispatch(self):
        client=self.client(lambda request:self.fail('no request expected'))
        path='/openApi/swap/v2/user/commissionRate'
        client.path_cd={};client.stats={'wait':0}
        client.buckets={'private':SimpleNamespace(take=lambda:0)}
        def waiting():client.path_cd[path]=bx.time.time()+60;return .25
        client.path_buckets={path:SimpleNamespace(take=waiting)}
        self.assertFalse(client._take('private',path))

    def test_exchange_unblock_timestamp_is_honored_with_its_space(self):
        client=self.client(lambda request:self.fail('no request expected'))
        client.path_cd={};client.stats={'rl':0};client.cooldown_until=0
        client.err=SimpleNamespace(write=lambda *args,**kwargs:None)
        path='/openApi/swap/v2/user/commissionRate'
        with patch.object(bx.time,'time',return_value=1700000000):
            client._trip(path,{'code':100410,'msg':'will be unblocked after 1700000300000'})
        self.assertAlmostEqual(client.path_cd[path],1700000300.4)


if __name__=='__main__':unittest.main(verbosity=2)
