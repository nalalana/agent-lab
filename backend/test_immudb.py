from immudb import ImmudbClient
json1 = {
    'created_at': '1741784965.31927',
    'id': 1,
    'is_valid': 1,
    'member':
        {
            'avatar': 'avatars/1_1741784946_jojo.jpg',
            'bio': '这是个人主页简介',
            'created_at': '1741784831.6833162',
            'email': '3392284556@qq.com',
            'id': 1,
            'is_activate': 1,
            'updated_at': '1741784946.7008262',
            'user_password': 'da8e4418bec3434e84b1a2a753dec25e',
            'username': 'xudawei'
        },
    'permission_level': 'write',
    'table':
        {
            'created_at': '1741784965.2745984',
            'description': '这是表格简介',
            'header': {
                'column0': {'key': 'id', 'none': False, 'type': 'string'},
                'column1': {'key': 'name', 'none': False, 'type': 'string'},
                'column2': {'key': 'age', 'none': False, 'type': 'int'},
                'column3': {'key': 'salary', 'none': True, 'type': 'float'}
            },
            'id': 1,
            'owner':
                {
                    'avatar': 'avatars/1_1741784946_jojo.jpg',
                    'bio': '这是个人主页简介',
                    'created_at': '1741784831.6833162',
                    'email': '3392284556@qq.com',
                    'id': 1,
                    'is_activate': 1,
                    'updated_at': '1741784946.7008262',
                    'user_password': 'da8e4418bec3434e84b1a2a753dec25e',
                    'username': 'xudawei'
                },
            'permission_level': 'public',
            'updated_at': '1741784965.2745984',
            'user_table_name': 'test1'
        },
    'team__id__team_team__fg': None,
    'updated_at': '1741784965.31927'
}
myclient = ImmudbClient('localhost:3322')
myclient.login('immudb', 'immudb')
myclient.createDatabase('gate16fd27068baf433b82eb8c7fada847da')