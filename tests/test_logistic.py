import numpy as np

from mce.research.logistic import fit_logistic, predict_proba


def _toy(n=400):
    """x1 の符号でほぼ決まる2値問題(決定的に生成)。"""
    x1 = np.array([np.sin(i * 0.37) for i in range(n)])
    x2 = np.array([np.cos(i * 0.91) for i in range(n)])
    noise = np.array([np.sin(i * 7.13) * 0.3 for i in range(n)])
    y = ((x1 + noise) > 0).astype(float)
    X = np.column_stack([x1, x2])
    return X, y


def test_learns_direction():
    X, y = _toy()
    w = fit_logistic(X, y)
    assert w[1] > 1.0  # x1 に正の重み
    assert abs(w[2]) < abs(w[1]) * 0.3  # 無関係な x2 の重みは小さい
    p = predict_proba(w, X)
    acc = ((p > 0.5) == (y > 0.5)).mean()
    assert acc > 0.85


def test_deterministic():
    X, y = _toy()
    w1 = fit_logistic(X, y)
    w2 = fit_logistic(X, y)
    assert np.array_equal(w1, w2)


def test_ridge_bounds_weights_on_separable_data():
    # 完全分離データでも ridge で重みが発散しない
    X = np.linspace(-1, 1, 100).reshape(-1, 1)
    y = (X[:, 0] > 0).astype(float)
    w = fit_logistic(X, y, l2=1e-3)
    assert np.all(np.isfinite(w))
    assert abs(w[1]) < 1e3


def test_balanced_prior_gives_half_proba():
    X = np.zeros((50, 1))
    y = np.array([1.0, 0.0] * 25)
    w = fit_logistic(X, y)
    p = predict_proba(w, np.zeros((1, 1)))
    assert abs(p[0] - 0.5) < 1e-6
