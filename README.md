# Data Mining - COMP4040

## Group member
Nguyen Tung Lam - 21lam.nt@vinuni.edu.vn \
Vu Duy Tung - 21tung.vd@vinuni.edu.vn \
Ta Viet Thang - 21thang.tv@vinuni.edu.vn

## Preparation
- Install dependencies
```
pip install -r requirements.txt
```

- Download EB-NeRD dataset, a dataset of Ekstra Bladet News Recommendation from this [source](https://recsys.eb.dk/dataset/)

- Preprocess the data into appropriate format
```
python ebnerd_preprocess.py
```

## Train a fairness-aware recommendation system:
```
python run_recbole.py
```


## Acknowledgement
This code is mostly inherited from RecBole-FairRec library.
```
@inproceedings{zhao2021recbole,
  title={Recbole: Towards a unified, comprehensive and efficient framework for recommendation algorithms},
  author={Wayne Xin Zhao and Shanlei Mu and Yupeng Hou and Zihan Lin and Kaiyuan Li and Yushuo Chen and Yujie Lu and Hui Wang and Changxin Tian and Xingyu Pan and Yingqian Min and Zhichao Feng and Xinyan Fan and Xu Chen and Pengfei Wang and Wendi Ji and Yaliang Li and Xiaoling Wang and Ji-Rong Wen},
  booktitle={{CIKM}},
  year={2021}
}
```
