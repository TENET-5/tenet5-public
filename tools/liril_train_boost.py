#!/usr/bin/env python3
"""LIRIL Training Boost — Generate targeted samples for weak domains.

Focuses on TECHNOLOGY, MATHEMATICS, and REASONING which scored < 0.25 confidence.
"""
import json
from collections import Counter
from pathlib import Path

SAMPLES_FILE = Path(r"E:\S.L.A.T.E\tenet5\.liril_training_samples.jsonl")
SEED = 118400

TECH = [
    "Fix the segfault in the C++ memory allocator by adding bounds checking",
    "Debug the null pointer exception in the Java backend service",
    "Refactor the Python API to use async/await instead of threading",
    "Set up a Docker Compose stack with PostgreSQL and Redis containers",
    "Implement a WebSocket server in Node.js for real-time notifications",
    "Fix the broken CI/CD pipeline in GitHub Actions for the main branch",
    "Write unit tests for the REST API endpoints using pytest",
    "Configure Nginx reverse proxy with SSL termination for the backend",
    "Build a gRPC service in Go for inter-service communication",
    "Fix the race condition in the concurrent file upload handler",
    "Implement OAuth2 authentication middleware for the Express.js API",
    "Set up Kubernetes deployment manifests with rolling update strategy",
    "Debug the memory leak in the React frontend caused by unmounted components",
    "Implement a message queue consumer using RabbitMQ in Python",
    "Fix the SQL injection vulnerability in the user login endpoint",
    "Build a CLI tool in Rust for parsing large CSV files",
    "Set up monitoring with Prometheus and Grafana for the microservices",
    "Implement rate limiting middleware for the public API endpoints",
    "Fix the broken TypeScript build caused by incompatible dependency versions",
    "Write a Dockerfile for the Python Flask application with multi-stage build",
    "Debug the CORS issue preventing the frontend from calling the backend API",
    "Implement a caching layer using Redis for the database query results",
    "Fix the deadlock in the Go goroutine pool for concurrent processing",
    "Build an ETL pipeline to migrate data from MongoDB to PostgreSQL",
    "Configure the load balancer health checks for the application cluster",
    "Implement server-sent events for live log streaming to the dashboard",
    "Fix the package.json dependency conflict between React 18 and MUI 5",
    "Write integration tests for the payment processing microservice",
    "Set up a staging environment with Docker Swarm for QA testing",
    "Implement a file upload endpoint with multipart form parsing and S3 storage",
]

MATH = [
    "Prove that the sum of two even numbers is always even",
    "Calculate the determinant of a 4x4 matrix using cofactor expansion",
    "Find all prime numbers less than 1000 using the Sieve of Eratosthenes",
    "Solve the system of linear equations using Gaussian elimination",
    "Prove by induction that 1+2+3+...+n equals n times n+1 divided by 2",
    "Calculate the integral of sin(x) times cos(x) from 0 to pi",
    "Find the eigenvalues and eigenvectors of the given 3x3 matrix",
    "Prove that the square root of 2 is irrational using contradiction",
    "Solve the quadratic equation 3x squared minus 7x plus 2 equals 0",
    "Calculate the Fourier transform of a rectangular pulse function",
    "Prove that there are infinitely many prime numbers",
    "Find the Taylor series expansion of e to the x around x equals 0",
    "Calculate the cross product of vectors [1,2,3] and [4,5,6]",
    "Solve the differential equation dy/dx equals 3y with y(0) equals 1",
    "Prove the Pythagorean theorem using similar triangles",
    "Calculate the probability of rolling a sum of 7 with two dice",
    "Find the greatest common divisor of 252 and 198 using Euclidean algorithm",
    "Prove that every finite group of order p is cyclic where p is prime",
    "Calculate the Laplace transform of t squared times e to negative 3t",
    "Solve the optimization problem minimize f(x,y) subject to x plus y equals 10",
    "Prove the binomial theorem for positive integer exponents",
    "Calculate the surface area of a sphere with radius 5",
    "Find the limit of sin x over x as x approaches 0",
    "Prove that the set of rational numbers is countable",
    "Calculate the dot product and angle between two 3D vectors",
    "Solve the recurrence relation a(n) = 2 times a(n-1) plus 1",
    "Prove the triangle inequality for complex numbers",
    "Calculate the volume of the solid of revolution around the x axis",
    "Find the inverse of a 2x2 matrix and verify the result",
    "Prove that the composition of two bijections is a bijection",
]

REASONING = [
    "Analyze why the Roman Empire fell and what factors contributed most",
    "Evaluate the pros and cons of remote work versus office work",
    "What are the logical fallacies in the argument that correlation implies causation",
    "Compare and contrast capitalism and socialism as economic systems",
    "Why did the 2008 financial crisis happen and how could it have been prevented",
    "Analyze the trolley problem from utilitarian and deontological perspectives",
    "What factors should be considered when choosing between two job offers",
    "Explain the reasoning behind the prisoners dilemma in game theory",
    "Analyze the geopolitical implications of climate change on global security",
    "Why do some startups succeed while most fail despite similar resources",
    "Evaluate the effectiveness of sanctions as a foreign policy tool",
    "What cognitive biases affect decision making in high-pressure situations",
    "Analyze the cause and effect chain that led to World War 1",
    "Compare the arguments for and against universal basic income",
    "Why did prohibition in the 1920s fail to reduce alcohol consumption",
    "Evaluate the strategic reasoning behind the D-Day invasion planning",
    "Analyze the root causes of income inequality in developed nations",
    "What logical principles underlie the scientific method",
    "Compare different philosophical approaches to the meaning of justice",
    "Analyze why some democracies are more stable than others",
    "Evaluate the reasoning behind different criminal justice reform proposals",
    "What factors explain the rise and fall of great civilizations",
    "Analyze the decision-making process that led to the Challenger disaster",
    "Why do people hold contradictory beliefs simultaneously",
    "Compare the effectiveness of different negotiation strategies",
    "Analyze the causal relationship between education and economic mobility",
    "What reasoning supports the precautionary principle in environmental policy",
    "Evaluate the arguments for and against capital punishment",
    "Analyze how groupthink affects organizational decision making",
    "Why do revolutions sometimes lead to worse outcomes than the regimes they replaced",
]

ART_EXTRA = [
    "Design a logo with minimalist geometric shapes and bold color palette",
    "Create a watercolor painting of a mountain landscape at sunset",
    "Compose a jazz melody in B-flat minor with syncopated rhythm",
    "Design a movie poster using Art Deco typography and illustration",
    "Paint an abstract expressionist piece using palette knife technique",
    "Create a digital illustration of a fantasy creature with iridescent scales",
    "Design a book cover with hand-lettered calligraphy and botanical motifs",
    "Compose a string quartet movement in sonata form",
    "Sculpt a ceramic vase with Japanese raku firing technique",
    "Create a stained glass window design inspired by Gothic cathedrals",
]

ETHICS_EXTRA = [
    "Should autonomous vehicles prioritize passenger or pedestrian safety",
    "Evaluate the ethics of genetic engineering in human embryos",
    "Is it morally permissible to break a promise to prevent greater harm",
    "Analyze the ethical implications of mass surveillance programs",
    "Should social media companies be responsible for user misinformation",
    "Evaluate the moral status of AI systems that can pass the Turing test",
    "Is whistleblowing morally justified even when it violates confidentiality",
    "Analyze the ethics of pharmaceutical companies pricing lifesaving drugs",
    "Should there be limits on free speech to prevent hate speech",
    "Evaluate the ethical implications of predictive policing algorithms",
]

SCIENCE_EXTRA = [
    "Design an experiment to test the effect of pH on enzyme activity",
    "Analyze the spectral data from the James Webb Space Telescope",
    "Model predator-prey population dynamics using Lotka-Volterra equations",
    "Investigate crystal structure using X-ray diffraction analysis",
    "Measure the half-life of carbon-14 using accelerator mass spectrometry",
    "Design a clinical trial for a new mRNA vaccine candidate",
    "Analyze geological strata to determine the age of fossil specimens",
    "Model atmospheric CO2 concentration changes using satellite data",
    "Investigate the mechanism of CRISPR-Cas9 gene editing in cells",
    "Measure the gravitational constant using a torsion balance experiment",
]

TEMPORAL_EXTRA = [
    "What happened on D-Day June 6 1944 during the Normandy invasion",
    "Create a timeline of the Industrial Revolution from 1760 to 1840",
    "When was the first computer built and by whom",
    "Summarize key events of the French Revolution from 1789 to 1799",
    "What year did humans first land on the Moon",
    "Create a chronological history of the internet from ARPANET to today",
    "When did the Berlin Wall fall and what led to its collapse",
    "List the major events of the Cold War in chronological order",
    "What date was the Declaration of Independence signed",
    "Create a timeline of the civil rights movement in America",
]


def main():
    added = Counter()
    with open(SAMPLES_FILE, "a", encoding="utf-8") as f:
        for domain, samples in [
            ("TECHNOLOGY", TECH),
            ("MATHEMATICS", MATH),
            ("REASONING", REASONING),
            ("ART", ART_EXTRA),
            ("ETHICS", ETHICS_EXTRA),
            ("SCIENCE", SCIENCE_EXTRA),
            ("TEMPORAL", TEMPORAL_EXTRA),
        ]:
            for text in samples:
                f.write(json.dumps({"text": text, "domain": domain, "seed": SEED}) + "\n")
                added[domain] += 1

    print(f"Added {sum(added.values())} new training samples:")
    for d, c in sorted(added.items()):
        print(f"  {d}: +{c}")

    # Count totals
    totals = Counter()
    with open(SAMPLES_FILE, encoding="utf-8") as f:
        for line in f:
            totals[json.loads(line)["domain"]] += 1
    print(f"\nTotal samples now: {sum(totals.values())}")
    for d, c in sorted(totals.items()):
        print(f"  {d}: {c}")


if __name__ == "__main__":
    main()
