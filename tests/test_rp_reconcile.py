import unittest

from matchlog.rp_progress import build_progress
from matchlog.rp_reconcile import rule_estimate
from matchlog.recognition import parse_rank


class RPReview(unittest.TestCase):
    config = {'season': 'S31', 'self_name': 'PlayerOne'}

    def match(self, ident, at, total, delta, **kw):
        return {'id': ident, 'created': at, 'rank': {'total_rp': total, 'delta': delta, **kw}}

    def test_promotion_between_real_reads_and_tally_animation(self):
        first = self.match('first', 100, 3812, 82)
        tally = self.match('early', 200, 3792, -18, tally_pending=True,
                           breakdown={'combat_rp':0,'placement_rp':0,'bonus_rp':0,'entry_cost':20})
        missing = self.match('second', 360, None, None)
        missing.update(rp_order_at=300, summary={'placement':2,'players':[{'name':'[TAG] PlayerOne','kills':3,'assists':4,'participation':1}]})
        later = self.match('later', 350, 4012, -10)
        tail = self.match('tail', 400, 4088, 76)
        lobby = {'created':410,'source':'lobby','total_rp':4088}
        result = build_progress(self.config,[first,tally,missing,later,tail],[lobby],now=450)
        self.assertEqual(result['review']['early']['value'], -20)
        self.assertEqual(result['review']['early']['raw'], -18)
        self.assertEqual(result['review']['second']['value'], 230)
        self.assertEqual(result['recent_net']['value'], 358)
        self.assertEqual(result['recent_net']['source'], 'lobby')
        self.assertEqual(result['recent_net']['unattributed'], 0)
        self.assertEqual(result['total_rp'], 4088)

    def test_two_missing_show_a_combined_gap(self):
        matches = [self.match('first',100,3792,-20), self.match('a',200,None,None),
                   self.match('b',300,None,None), self.match('last',350,4012,-10)]
        result = build_progress(self.config,matches,[],now=400)
        self.assertFalse(result['review'])
        self.assertEqual(result['recent_net']['unattributed'],230)
        self.assertEqual(result['recent_net']['missing'],2)

    def test_missing_fee_and_transient_display_need_an_independent_read(self):
        first = self.match('first',100,4067,-15)
        tail = self.match('tail',200,4088,14,tally_pending=True,
                          breakdown={'combat_rp':36,'placement_rp':5,'bonus_rp':0,'entry_cost':None})
        result = build_progress(self.config,[tail],[],now=300)
        self.assertIsNone(result['review']['tail']['value'])
        result = build_progress(self.config,[first,tail],[],now=300)
        self.assertEqual(result['review']['tail']['value'],21)
        tail['rp_manual'] = True
        result = build_progress(self.config,[first,tail],[],now=300)
        self.assertNotIn('tail',result['review'])

    def test_rules_use_actual_entry_fee_when_known_and_stop_at_season_boundary(self):
        self.assertEqual(rule_estimate('S31',2,3,4,1,before_rank='白银 III')['value'],230)
        self.assertEqual(rule_estimate('S31',2,3,4,1,entry_cost=10,before_rank='白银 III')['value'],240)
        self.assertIsNone(rule_estimate('S31',2,3,4,None,entry_cost=20))
        self.assertIsNone(rule_estimate('S32',2,3,4,1,entry_cost=20))

    def test_protection_counter_is_not_rp_and_refund_is_already_in_net(self):
        tokens = [('失去的排位点数',.464,.212,.55,.23),('-53',.463,.182,.49,.205),
                  ('0/3',.486,.248,.52,.27),('降级',.535,.249,.56,.26),('保护',.536,.266,.56,.28),
                  ('战斗排位点数总计',.029,.480,.14,.49),('0排位点数',.246,.478,.31,.49),
                  ('奖励排位点数总计',.361,.479,.47,.49),('37排位点数',.579,.478,.63,.49),
                  ('定级排位点数总计',.696,.480,.8,.49),('-90排位点数',.914,.478,.97,.49),
                  ('参赛花费',.694,.556,.75,.57),('Apex 猎杀者-90排位点数',.856,.557,.97,.57),
                  ('降级保护',.361,.589,.42,.60),('37排位点数',.580,.591,.63,.60)]
        rank = parse_rank([dict(text=text,x=x,y=y,right=right,bottom=bottom) for text,x,y,right,bottom in tokens])
        self.assertEqual(rank['demotion_protection'],{'remaining':0,'capacity':3,'refund':37})
        self.assertIsNone(rank['progress'])
        self.assertEqual(rank['delta'],-53)
        self.assertFalse(rank['tally_pending'])

    def test_24_hours_reuses_review_and_includes_older_matches(self):
        a = self.match('a', 10000, 3500, -15)
        b = self.match('b', 52000, 3550, 50)
        result = build_progress(self.config,[a,b],[],now=55000)
        self.assertEqual(result['recent_net']['value'],50)
        self.assertEqual(result['recent_net_24']['value'],35)
