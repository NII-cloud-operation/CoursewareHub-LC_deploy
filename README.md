# CoursewareHub-LC_deploy

CoursewareHubの構築notebook

AWS上に、CoursewareHubの運用管理用の[OperationHub](https://github.com/NII-cloud-operation/OperationHub)と、CoursewareHubを構築するnotebookです。

## 構築方法

最初にDocker Engineとdocker-composeがインストールされた環境が必要です。

以下のような流れで構築します。

1. Notebookサーバーの起動
1. そのNotebookサーバーを使用して、OperationHubを構築
1. 構築したOperationHubを使用して、CourseareHubを構築


### Notebookサーバーの起動

このリポジトリをcloneし、以下のように、Notebookサーバーを起動します。

sudoの有無は環境に応じて読み替えてください。

    $ git clone https://github.com/NII-cloud-operation/CoursewareHub-LC_deploy
    $ cd CoursewareHub-LC_deploy
    $ sudo docker-compose up --build

以下のようなログが出力されます。

```
jupyter_1  | [C 08:07:38.436 NotebookApp]
jupyter_1  |
jupyter_1  |     To access the notebook, open this file in a browser:
jupyter_1  |         file:///home/jovyan/.local/share/jupyter/runtime/nbserver-7-open.html
jupyter_1  |     Or copy and paste one of these URLs:
jupyter_1  |         http://e6fe59168280:8888/?token=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
jupyter_1  |      or http://127.0.0.1:8888/?token=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

http://127.0.0.1:8888/?token=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx にアクセスします。

ローカルホスト以外で起動した場合は、ホスト名は読み替えてください。

### OperationHubの構築

Notebookサーバーにログインしたら、「D01_OperationHubをAWSに構築.ipynb」を実行し、OperationHubを構築します。

### CoursewareHubの構築

構築したOperationHubにログインし、以下のNotebookを順に実行して、CoursewareHubを構築します。

1. D01_CoursewareHub用VMをAWSに作成.ipynb
1. D02_CoursewareHubインベントリの準備.ipynb
1. D03_NFSの準備.ipynb
1. D04_Docker Swarmの準備.ipynb
1. D05_CoursewareHubの準備.ipynb

「D06_CoursewareHubコンテンツの配備.ipynb」は、構築したCoursewareHub上で実行してください。

