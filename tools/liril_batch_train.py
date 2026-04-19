from tenet.win_asyncio import run as _win_run  # Windows nats-py fix
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
"""Batch train LIRIL classifier on all ARTSTEM domains."""
import asyncio
import json
import os
import nats.aio.client as nc

SAMPLES = {
    "MATHEMATICS": [
        "Implement Cholesky decomposition for positive-definite matrices",
        "Prove that every continuous function on a closed interval is uniformly continuous",
        "Calculate the volume of an n-dimensional hypersphere",
        "Implement the extended Euclidean algorithm for GCD computation",
        "Derive the formula for the sum of geometric series",
        "Prove the binomial theorem using mathematical induction",
        "Implement Gauss-Jordan elimination for matrix inversion",
        "Calculate the Riemann zeta function at even positive integers",
        "Implement the Chinese Remainder Theorem algorithm",
        "Prove that there are infinitely many prime numbers",
        "Calculate the eigendecomposition of a covariance matrix",
        "Implement the fast inverse square root algorithm",
        "Derive the Euler-Lagrange equation from calculus of variations",
        "Prove the central limit theorem for i.i.d. random variables",
        "Implement numerical solutions to ODEs using Runge-Kutta method",
    ],
    "REASONING": [
        "Determine why a distributed system exhibits split-brain syndrome",
        "Design a conflict resolution strategy for concurrent CRDT edits",
        "Analyze the tradeoffs of write-ahead logging vs shadow paging",
        "Evaluate the correctness of a lock-free concurrent queue implementation",
        "Design a backpressure mechanism for an async pipeline",
        "Determine the optimal retry strategy with exponential backoff and jitter",
        "Analyze why a particular hash function produces clustering",
        "Design a priority inheritance protocol to prevent priority inversion",
        "Evaluate the impact of false sharing on cache line performance",
        "Determine the root cause of a thundering herd problem",
        "Design a resource allocation algorithm that prevents deadlock",
        "Analyze the consistency guarantees of a read-your-writes session",
        "Evaluate whether a given finite state machine is minimal",
        "Design an incremental garbage collector with low pause times",
        "Determine the optimal thread pool size for mixed CPU/IO workloads",
    ],
    "ART": [
        "Design a procedural terrain generator with erosion simulation",
        "Create a physically-based rendering shader for metallic surfaces",
        "Implement a skeletal animation system with inverse kinematics",
        "Design a particle system for realistic fire and smoke effects",
        "Create a procedural city generator with building variations",
        "Implement cloth simulation using mass-spring model",
        "Design a non-photorealistic rendering pipeline for cel shading",
        "Create a procedural texture generator using Perlin noise octaves",
        "Implement a real-time shadow mapping system with soft shadows",
        "Design a level-of-detail system for large open world rendering",
        "Create a volumetric fog rendering system using ray marching",
        "Implement a deferred rendering pipeline with multiple light sources",
        "Design a GPU-accelerated fluid simulation using SPH method",
        "Create a post-processing pipeline with bloom DOF and motion blur",
        "Implement a tessellation shader for adaptive mesh subdivision",
    ],
    "SCIENCE": [
        "Simulate molecular dynamics using Lennard-Jones potential",
        "Implement a genetic algorithm for protein folding optimization",
        "Design a Monte Carlo simulation for neutron transport",
        "Create a climate model using finite difference methods",
        "Implement a neural ODE solver for chemical reaction kinetics",
        "Design a spectral analysis pipeline for astronomical data",
        "Simulate electromagnetic wave propagation using FDTD method",
        "Implement a phylogenetic tree reconstruction algorithm",
        "Design a sensor fusion algorithm for IMU and GPS data",
        "Create a computational fluid dynamics solver for airfoil analysis",
    ],
    "TEMPORAL": [
        "Design a distributed event sourcing system with snapshot compaction",
        "Implement a vector clock algorithm for causal ordering",
        "Create a time-series anomaly detection system using LSTM",
        "Design a temporal database with bitemporal querying support",
        "Implement a job scheduler with dependency DAG resolution",
        "Create a changelog tracking system with efficient diffing",
        "Design a release pipeline with canary deployments and auto-rollback",
        "Implement a rate limiter using sliding window log algorithm",
        "Create a deadline-aware task queue with priority aging",
        "Design a temporal workflow engine with compensating transactions",
    ],
    "ETHICS": [
        "Design a privacy-preserving federated learning system",
        "Implement differential privacy for aggregate query results",
        "Create a consent management system compliant with GDPR",
        "Design an audit trail system with tamper-evident logging",
        "Implement secure multi-party computation for private set intersection",
        "Create a bias detection pipeline for ML model fairness evaluation",
        "Design a data anonymization system using k-anonymity",
        "Implement homomorphic encryption for secure cloud computation",
        "Create a transparent AI decision explanation system",
        "Design a secure voting system with verifiable results",
    ],
    "NATURE": [
        "Simulate predator-prey population dynamics using Lotka-Volterra",
        "Implement an ecosystem diversity index calculator",
        "Design a wildlife migration pattern tracker using GPS telemetry",
        "Create a soil erosion model for watershed management",
        "Implement a forest fire spread simulation using cellular automata",
        "Design a marine biodiversity assessment using eDNA analysis",
        "Create a pollinator network visualization tool",
        "Implement a species distribution model using MaxEnt",
        "Design a water quality monitoring system with IoT sensors",
        "Create a carbon sequestration calculator for reforestation projects",
    ],
}


async def main():
    c = nc.Client()
    nats_url = os.environ.get("NATS_URL", os.environ.get("TENET_NATS_URL", "nats://127.0.0.1:4223"))
    await c.connect(nats_url)
    total = 0
    for domain, samples in SAMPLES.items():
        for text in samples:
            await c.publish(
                "tenet5.liril.train",
                json.dumps(
                    {
                        "task": text,
                        "domain": domain,
                        "result_status": "classified",
                        "quality": 1.0,
                        "seed": 118400,
                        "agent": "claude_code",
                    }
                ).encode(),
            )
            total += 1
        print(f"  {domain}: {len(samples)} samples")
    await c.flush()
    print(f"Pushed {total} total training samples")

    # Trigger retrain
    await asyncio.sleep(2)
    try:
        msg = await c.request("tenet5.liril.retrain", b"{}", timeout=30)
        print(f"RETRAIN: {msg.data.decode()[:200]}")
    except Exception as e:
        print(f"RETRAIN: {e} (samples stored, will retrain on next cycle)")

    # Final status
    try:
        msg = await c.request("tenet5.liril.status", b"{}", timeout=5)
        d = json.loads(msg.data.decode())
        stats = d["stats"]
        print(
            f"LIRIL: classified={stats['classified']} trained={stats['trained']} "
            f"samples={d.get('training_samples', '?')}"
        )
    except Exception as e:
        print(f"STATUS: {e} (LIRIL busy, samples were pushed OK)")
    await c.close()


if __name__ == "__main__":
    _win_run(main())
