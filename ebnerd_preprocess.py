import os
import pandas as pd

if __name__ == "__main__":
    
    pth_to_data = "~/dataset/ebnerd_small"

    pth_to_behaviors = os.path.join(pth_to_data, "train", "behaviors.parquet")
    
    pth_to_history = os.path.join(pth_to_data, "train", "history.parquet")

    pth_to_items = os.path.join(pth_to_data, "articles.parquet")

    behaviors = pd.read_parquet(pth_to_behaviors)

    interactions = behaviors[['user_id', 'article_id']]
    inter_header = ["user_id:token", "item_id:token"]
    interactions.dropna(inplace=True)
    interactions.to_csv("ebnerd_test.inter", header=inter_header, index=False)


    articles = pd.read_parquet(pth_to_items)
    articles = articles[['article_id', 'title', 'category_str']]
    articles_header = ['item_id:token',	'news_title:token_seq',	'genre:token']
    articles.dropna(inplace=True)
    articles.to_csv("ebnerd_test.item", header=articles_header, index=False)

    users = behaviors[['user_id', 'gender', 'age']]
    users_header = ['use_id:token',	'gender:float',	'age:float']
    users.dropna(inplace=True)
    users.to_csv("ebnerd_test.user", header=users_header, index=False)