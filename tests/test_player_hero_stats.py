import unittest

from matchlog.player_stats import player_hero_stats


def match(ident, placement=None, players=None, **fields):
    summary = {'placement': placement, 'players': players} if players is not None else None
    return {'id': ident, 'summary': summary, **fields}


class HeroStatsTests(unittest.TestCase):
    config = {'self_name': 'Owner', 'friends': ['Friend', 'Absent', '[T] Owner', '[T] Friend']}

    def test_unknown_zero_and_samples_stay_distinct(self):
        records = [
            match('a', 1, [{'name': 'Owner', 'hero': '艾瑟儿', 'damage': 0, 'kills': 0},
                           {'name': 'Friend', 'hero': '万蒂奇'}]),
            match('b', 5, [{'name': '[TAG] Owner', 'hero': 'Axle', 'damage': 200, 'assists': 2},
                           {'name': 'Friend', 'hero': None}]),
            match('c', None, [{'name': 'Owner', 'hero': '未识别', 'damage': 100}]),
            match('d'),
        ]
        owner, friend, absent = player_hero_stats(records, self.config)
        self.assertEqual((owner['games'], owner['known_hero_games'], owner['unknown_hero_games']), (4, 2, 2))
        self.assertEqual((owner['wins'], owner['placement_games']), (1, 2))
        hero, = owner['heroes']
        self.assertEqual((hero['name'], hero['games'], hero['match_ids']), ('艾瑟儿', 2, ['a', 'b']))
        self.assertEqual((hero['damage'], hero['damage_games']), (100, 2))
        self.assertEqual((hero['kills'], hero['kills_games']), (0, 1))
        self.assertEqual((hero['assists'], hero['assists_games']), (2, 1))
        self.assertEqual((hero['wins'], hero['average_placement'], hero['placement_games']), (1, 3, 2))
        self.assertEqual((friend['games'], friend['unknown_hero_games']), (2, 1))
        self.assertEqual(absent['heroes'], [])
        self.assertEqual(absent['placement_games'], 0)

    def test_friends_require_same_game_owner_and_each_match_counts_once(self):
        both = match('a', 2, [{'name': 'Owner', 'hero': 'vantage'}, {'name': 'Friend', 'hero': 'loba'},
                               {'name': '[T] Friend', 'hero': 'mirage'}, {'name': 'Stranger', 'hero': 'wraith'}])
        records = [both, both, match('b', None, [{'name': 'Friend', 'hero': 'loba'}]),
                   match('c', 1, [{'name': 'Owner', 'hero': 'loba'}], merged_into='a')]
        owner, friend, _ = player_hero_stats(records, self.config)
        self.assertEqual([row['name'] for row in friend['heroes']], ['罗芭'])
        self.assertEqual((friend['games'], friend['wins'], friend['placement_games']), (1, 0, 1))
        self.assertEqual((owner['games'], owner['unknown_hero_games']), (2, 1))

    def test_no_local_profile_does_not_publish_teammate_stats(self):
        self.assertEqual(player_hero_stats([], {'self_name': '', 'friends': ['Friend']}), [])


if __name__ == '__main__':
    unittest.main()
