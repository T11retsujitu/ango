"""決定的なロジスティック回帰(IRLS / Newton 法、ridge 正則化)。

sklearn / xgboost を導入せず numpy のみで実装する(軽量構成の維持)。
乱数を一切使わないため、同一データ・同一設定なら結果は bit 単位で一致する。
入力 X は呼び出し側で標準化しておくこと(train 統計のみを使う。test への
標準化統計の混入は leakage)。
"""

import numpy as np


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1e-3, max_iter: int = 25, tol: float = 1e-10) -> np.ndarray:
    """重み [bias, w1..wd] を返す。y は {0,1}。bias は正則化しない。

    ridge(l2)により完全分離データでも重みが発散しない。反復は決定的。
    """
    n, d = X.shape
    Xb = np.hstack([np.ones((n, 1)), X])
    w = np.zeros(d + 1)
    reg = np.full(d + 1, l2)
    reg[0] = 0.0
    for _ in range(max_iter):
        z = np.clip(Xb @ w, -35.0, 35.0)
        p = 1.0 / (1.0 + np.exp(-z))
        grad = Xb.T @ (p - y) / n + reg * w
        weight = np.maximum(p * (1.0 - p), 1e-9)
        hess = (Xb * weight[:, None]).T @ Xb / n + np.diag(reg)
        step = np.linalg.solve(hess, grad)
        w = w - step
        if float(np.max(np.abs(step))) < tol:
            break
    return w


def predict_proba(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    """P(y=1 | X)。X は fit 時と同じ標準化を適用済みであること。"""
    Xb = np.hstack([np.ones((len(X), 1)), X])
    z = np.clip(Xb @ w, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-z))
