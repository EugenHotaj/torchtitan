# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-unsafe
import logging

import numpy as np
import torch

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Import grouped GEMM implementations
try:
    from mg_grouped_gemm import grouped_gemm_backward, grouped_gemm_forward

except ImportError:
    logging.error(
        "Error importing grouped GEMM modules. Make sure the implementation files are in the correct path."
    )
    raise


def compute_reference_forward(x, w, m_sizes):
    """
    Compute reference forward pass using PyTorch operations.

    Args:
        x (torch.Tensor): Input tensor of shape (M, K)
        w (torch.Tensor): Weight tensor of shape (N, K)
        m_sizes (torch.Tensor): Group sizes tensor of shape (G)

    Returns:
        torch.Tensor: Reference output tensor of shape (M, N)
    """
    result = torch.zeros((x.shape[0], w.shape[0]), dtype=x.dtype, device=x.device)

    m_start = 0
    for g in range(len(m_sizes)):
        m_size = m_sizes[g].item()
        if m_size > 0:
            m_end = m_start + m_size

            # Extract group input
            x_g = x[m_start:m_end]

            # Compute group output: y_g = x_g @ w.T
            y_g = torch.matmul(x_g, w.T)

            # Store result
            result[m_start:m_end] = y_g

            # Update start index
            m_start = m_end

    return result


def compute_reference_backward(x, w, m_sizes, grad_output):
    """
    Compute reference backward pass using PyTorch autograd.

    Args:
        x (torch.Tensor): Input tensor of shape (M, K)
        w (torch.Tensor): Weight tensor of shape (N, K)
        m_sizes (torch.Tensor): Group sizes tensor of shape (G)
        grad_output (torch.Tensor): Gradient tensor of shape (M, N)

    Returns:
        tuple: (grad_x, grad_w) gradient tensors
    """
    # Create autograd-enabled copies
    x_autograd = x.detach().clone().requires_grad_(True)
    w_autograd = w.detach().clone().requires_grad_(True)

    # Compute forward pass
    output = compute_reference_forward(x_autograd, w_autograd, m_sizes)

    # Backpropagate
    output.backward(grad_output)

    return x_autograd.grad, w_autograd.grad


def analyze_tensor_differences(actual, expected, name):
    """
    Analyze differences between actual and expected tensors.

    Args:
        actual (torch.Tensor): Actual tensor
        expected (torch.Tensor): Expected tensor
        name (str): Name of the tensor for logging

    Returns:
        bool: True if tensors are close enough
    """
    rtol = 0.5  # Relative tolerance for float16
    atol = 0.5  # Absolute tolerance for float16

    # Analyze differences
    diff = (actual - expected).abs()
    max_idx = diff.argmax().item()
    idx = np.unravel_index(max_idx, actual.shape)
    max_diff = diff.max().item()

    logging.info(f"Largest {name} difference: {max_diff} at {idx}")
    logging.info(f"Values: {actual[idx].item()} vs {expected[idx].item()}")

    is_close = torch.allclose(actual, expected, rtol=rtol, atol=atol)

    if is_close:
        logging.info(f"✓ SUCCESS: {name} matches PyTorch reference")
    else:
        logging.error(f"✗ FAILURE: {name} mismatch detected")

        # Count zeros
        zeros_actual = (actual == 0).sum().item()
        zeros_expected = (expected == 0).sum().item()
        logging.info(
            f"Zeros in {name} (actual): {zeros_actual}/{actual.numel()} ({zeros_actual/actual.numel()*100:.2f}%)"
        )
        logging.info(
            f"Zeros in {name} (expected): {zeros_expected}/{expected.numel()} ({zeros_expected/expected.numel()*100:.2f}%)"
        )

        # Check for NaNs
        nan_actual = torch.isnan(actual).sum().item()
        if nan_actual > 0:
            logging.error(f"NaN values detected in {name}: {nan_actual}")

    return is_close


def test_forward_pass():
    """
    A simple test for the M*G grouped GEMM forward pass with detailed error handling.

    In M*G grouping:
    - M dimension is partitioned into G groups (M_total = sum(M_sizes))
    - N dimension is the same for all groups
    """
    try:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        # Test parameters for DeepSeek-like models
        G = 1  # Number of groups
        M_sizes = [
            2048,
        ]  # 2048, 2048, 2048]  # Group sizes (will be adjusted)
        M_total = sum(M_sizes)  # Total M dimension
        N = 4096  # Output dimension (same for all groups)
        K = 7168  # Hidden dimension

        # Create group sizes tensor
        m_sizes = torch.tensor(M_sizes, device=device, dtype=torch.int32)

        # Create input and weight tensors - using float16 for higher precision
        x = torch.randn(M_total, K, dtype=torch.float16, device=device)
        w = torch.randn(N, K, dtype=torch.float16, device=device)

        # Log the setup
        logging.info(f"Test setup - G: {G}, M_total: {M_total}, N: {N}, K: {K}")
        logging.info(f"Group sizes: {m_sizes}")
        logging.info(f"Input x shape: {x.shape}")
        logging.info(f"Weight w shape: {w.shape}")

        # Run forward pass
        logging.info("Running forward pass with grouped GEMM")
        result = grouped_gemm_forward(x, w, m_sizes)
        logging.info(f"Forward result shape: {result.shape}")

        # Compute reference result
        logging.info("Computing reference result with PyTorch")
        reference_result = compute_reference_forward(x, w, m_sizes)

        # Compare results
        logging.info("Comparing with PyTorch reference")
        forward_close = analyze_tensor_differences(
            result, reference_result, "Forward output"
        )

        return forward_close

    except Exception as e:
        logging.error(f"Test failed with error: {e}")
        import traceback

        logging.error(traceback.format_exc())
        return False


def test_backward_pass():
    """
    A simple test for the M*G grouped GEMM backward pass with detailed error handling.

    In M*G grouping:
    - M dimension is partitioned into G groups (M_total = sum(M_sizes))
    - N dimension is the same for all groups
    """
    try:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        # Test parameters for DeepSeek-like models
        G = 4  # Number of groups
        M_sizes = [2048, 2048, 2048, 2048]  # Group sizes (will be adjusted)
        M_total = sum(M_sizes)  # Total M dimension
        N = 4096  # Output dimension (same for all groups)
        K = 7168  # Hidden dimension

        # Create group sizes tensor
        m_sizes = torch.tensor(M_sizes, device=device, dtype=torch.int32)

        # Create input and weight tensors - using float16 for higher precision
        x = torch.randn(
            M_total, K, dtype=torch.float16, device=device, requires_grad=True
        )
        w = torch.randn(N, K, dtype=torch.float16, device=device, requires_grad=True)

        # Log the setup
        logging.info(f"Test setup - G: {G}, M_total: {M_total}, N: {N}, K: {K}")
        logging.info(f"Group sizes: {m_sizes}")
        logging.info(f"Input x shape: {x.shape}")
        logging.info(f"Weight w shape: {w.shape}")

        # Step 1: Run forward pass
        logging.info("Running forward pass")
        result = grouped_gemm_forward(x, w, m_sizes)
        logging.info(f"Forward result shape: {result.shape}")

        # Create a gradient for backpropagation
        grad_output = torch.randn_like(result)
        logging.info(f"Created gradient with shape: {grad_output.shape}")

        # Step 2: Run backward pass directly
        logging.info("Running backward pass directly")
        grad_x, grad_w = grouped_gemm_backward(grad_output, x, w, m_sizes)

        # Verify gradient shapes
        logging.info(
            f"Gradient shapes - grad_x: {grad_x.shape}, grad_w: {grad_w.shape}"
        )

        # Step 3: Verify gradient computation using PyTorch's autograd
        logging.info("Running PyTorch reference implementation")

        # Compute reference gradients
        x_ref_grad, w_ref_grad = compute_reference_backward(x, w, m_sizes, grad_output)

        # Compare gradients
        logging.info("Comparing gradients with PyTorch reference")
        grad_x_close = analyze_tensor_differences(grad_x, x_ref_grad, "grad_x")
        grad_w_close = analyze_tensor_differences(grad_w, w_ref_grad, "grad_w")

        # Log overall result
        if grad_x_close and grad_w_close:
            logging.info("✓ SUCCESS: Gradients match the PyTorch reference")
        else:
            logging.error("✗ FAILURE: Gradient mismatch detected")

        return grad_x_close and grad_w_close

    except Exception as e:
        logging.error(f"Test failed with error: {e}")
        import traceback

        logging.error(traceback.format_exc())
        return False


def test_multiple_deepseek_configs():
    """
    Test multiple DeepSeek model configurations with both forward and backward pass verification.
    """
    # DeepSeek configurations: (G, M, K, N)
    configs = [
        (4, 8192, 7168, 4096),  # Config 1
        (4, 8192, 2048, 7168),  # Config 2
        (8, 4096, 7168, 4096),  # Config 3
        (8, 4096, 2048, 7168),  # Config 4
    ]

    results = []

    for config_idx, (G, M, K, N) in enumerate(configs):
        logging.info(f"\n\n===== Testing DeepSeek Config {config_idx+1} =====")
        logging.info(f"G={G}, M={M}, K={K}, N={N}")

        try:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

            # Create even group sizes
            base_size = M // G
            remainder = M % G
            M_sizes = [base_size + (1 if i < remainder else 0) for i in range(G)]
            m_sizes = torch.tensor(M_sizes, device=device, dtype=torch.int32)

            # Create input and weight tensors using float16 for higher precision
            x = torch.randn(
                M, K, dtype=torch.float16, device=device, requires_grad=True
            )
            w = torch.randn(
                N, K, dtype=torch.float16, device=device, requires_grad=True
            )

            logging.info(f"Input x shape: {x.shape}, Weight w shape: {w.shape}")

            # Run forward pass
            result = grouped_gemm_forward(x, w, m_sizes)
            logging.info(f"Forward result shape: {result.shape}")

            # ===== FORWARD PASS VERIFICATION =====
            # Compute reference forward result
            reference_result = compute_reference_forward(x, w, m_sizes)

            # Compare forward results
            forward_close = analyze_tensor_differences(
                result, reference_result, "Forward output"
            )

            # ===== BACKWARD PASS VERIFICATION =====
            # Create gradient for backpropagation
            grad_output = torch.randn_like(result)

            # Run backward pass
            grad_x, grad_w = grouped_gemm_backward(grad_output, x, w, m_sizes)

            # Compute reference gradients
            x_ref_grad, w_ref_grad = compute_reference_backward(
                x, w, m_sizes, grad_output
            )

            # Compare backward results
            grad_x_close = analyze_tensor_differences(grad_x, x_ref_grad, "grad_x")
            grad_w_close = analyze_tensor_differences(grad_w, w_ref_grad, "grad_w")

            # Overall config result
            backward_close = grad_x_close and grad_w_close
            config_success = forward_close and backward_close
            results.append(
                (config_idx + 1, config_success, forward_close, backward_close)
            )

            # Log overall config result
            if config_success:
                logging.info(f"✓ SUCCESS: Config {config_idx+1} passed all tests!")
            else:
                logging.error(
                    f"✗ FAILURE: Config {config_idx+1} failed one or more tests"
                )

        except Exception as e:
            logging.error(f"Config {config_idx+1} test failed with error: {e}")
            import traceback

            logging.error(traceback.format_exc())
            results.append((config_idx + 1, False, False, False))

    # Summary
    logging.info("\n===== Test Results Summary =====")
    for config_idx, overall_success, forward_success, backward_success in results:
        overall_status = "✓ PASSED" if overall_success else "✗ FAILED"
        forward_status = "✓ PASSED" if forward_success else "✗ FAILED"
        backward_status = "✓ PASSED" if backward_success else "✗ FAILED"

        logging.info(f"Config {config_idx}: {overall_status}")
        logging.info(f"  - Forward pass: {forward_status}")
        logging.info(f"  - Backward pass: {backward_status}")

    return all(overall_success for _, overall_success, _, _ in results)


if __name__ == "__main__":
    logging.info(
        "Running verification for both forward and backward pass of M*G grouped GEMM"
    )

    # Run basic forward pass test
    logging.info("\n===== Running basic forward pass test =====")
    success_forward = test_forward_pass()
    logging.info(f"Basic forward test {'succeeded' if success_forward else 'failed'}")

    # Run basic backward pass test
    logging.info("\n===== Running basic backward pass test =====")
    success_backward = test_backward_pass()
    logging.info(f"Basic backward test {'succeeded' if success_backward else 'failed'}")

    # Run multiple DeepSeek configs with forward and backward verification
    logging.info("\n===== Running tests for all DeepSeek configs =====")
    success_configs = test_multiple_deepseek_configs()
    logging.info(
        f"DeepSeek configs tests {'all succeeded' if success_configs else 'had failures'}"
    )

    # Overall result
    overall_success = success_forward and success_backward and success_configs
    logging.info(
        f"\nOverall test result: {'SUCCESS' if overall_success else 'FAILURE'}"
    )
