// Copyright 2024 Xanadu Quantum Technologies Inc.

// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at

//     http://www.apache.org/licenses/LICENSE-2.0

// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#pragma once

#include <map>

#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/Support/Debug.h"

#include "Catalyst/IR/CatalystDialect.h"
#include "Quantum/IR/QuantumOps.h"

using namespace llvm;
using namespace mlir;
using namespace catalyst;

namespace catalyst{

// The six Pauli eigenstates
enum class QubitState {
    ZERO,
    ONE,
    PLUS,
    MINUS,
    LEFT,
    RIGHT,
    NOT_A_BASIS,
};


// {input state : {gate, output state}}
static std::map<QubitState, std::map<StringRef, QubitState>> QubitTransitions = {
    {QubitState::ZERO,
     {
         {"Hadamard", QubitState::PLUS},
         {"PauliX", QubitState::ONE},
         {"PauliY", QubitState::ONE},
         {"PauliZ", QubitState::ZERO},
     }},

    {QubitState::ONE,
     {
         {"Hadamard", QubitState::MINUS},
         {"PauliX", QubitState::ZERO},
         {"PauliY", QubitState::ZERO},
         {"PauliZ", QubitState::ONE},
     }},

    {QubitState::PLUS,
     {
         {"Hadamard", QubitState::ZERO},
         {"PauliX", QubitState::PLUS},
         {"PauliY", QubitState::MINUS},
         {"PauliZ", QubitState::MINUS},
         {"S", QubitState::LEFT},
     }},

    {QubitState::MINUS,
     {
         {"Hadamard", QubitState::ONE},
         {"PauliX", QubitState::MINUS},
         {"PauliY", QubitState::PLUS},
         {"PauliZ", QubitState::PLUS},
         {"S", QubitState::RIGHT},
     }},

    {QubitState::LEFT,
     {
         {"Hadamard", QubitState::RIGHT},
         {"PauliX", QubitState::RIGHT},
         {"PauliY", QubitState::LEFT},
         {"PauliZ", QubitState::RIGHT},
         // We leave in S+ to indicate the FSM structure
         // The actual implementation is `quantum.custom "S"() %in {adjoint}`
         //{"S+", QubitState::PLUS},
     }},

    {QubitState::RIGHT,
     {
         {"Hadamard", QubitState::LEFT},
         {"PauliX", QubitState::LEFT},
         {"PauliY", QubitState::RIGHT},
         {"PauliZ", QubitState::LEFT},
         // We leave in S+ to indicate the FSM structure
         // The actual implementation is `quantum.custom "S"() %in {adjoint}`
         //{"S+", QubitState::MINUS},
     }},
};

class PropagateSimpleStatesAnalysis{
public:
	PropagateSimpleStatesAnalysis(Operation *target){
		// `target` is a qnode function
        target->walk([&](Operation *op) {
            if (op->getNumResults() != 1) {
                // restrict to single-qubit gates
                return;
            }

            Value res = op->getResult(0);
            if (!isa<quantum::QubitType>(res.getType())) {
                // not a qubit value
                return;
            }

            // starting qubits are in |0>
            if (isa<quantum::ExtractOp>(op)) {
                qubitValues[res] = QubitState::ZERO;
                return;
            }

            assert(isa<quantum::CustomOp>(op));

            // takes in parameters other than the parent qubit
            // e.g. the rotation angle
            // must be NOT_A_BASIS!
            if (op->getNumOperands() != 1) {
                qubitValues[res] = QubitState::NOT_A_BASIS;
                return;
            }

            // get state from parent and gate
            StringRef gate = cast<quantum::CustomOp>(op).getGateName();
            Value parent = op->getOperand(0);
            assert(qubitValues.contains(parent));

            // non basis states stay as non basis states
            if (qubitValues[parent] == QubitState::NOT_A_BASIS) {
                qubitValues[res] = QubitState::NOT_A_BASIS;
                return;
            }

            // Identity preserves parent state
            if (gate == "Identity") {
                qubitValues[res] = qubitValues[parent];
                return;
            }

            // A valid FSM transition gate
            // Special treatment for S+ gate from |L> and |R>
            if (((qubitValues[parent] == QubitState::LEFT) ||
                 (qubitValues[parent] == QubitState::RIGHT)) &&
                (gate == "S")) {
                if (op->hasAttr("adjoint")) {
                    switch (qubitValues[parent]) {
                    case QubitState::LEFT:
                        qubitValues[res] = QubitState::PLUS;
                        break;
                    case QubitState::RIGHT:
                        qubitValues[res] = QubitState::MINUS;
                        break;
                    default:
                        // this will never trigger as the switch is inside an if
                        break;
                    }
                }
                else {
                    qubitValues[res] = QubitState::NOT_A_BASIS;
                }
                return;
            }

            // A valid FSM transition gate
            if (QubitTransitions[qubitValues[parent]].count(gate) == 1) {
                qubitValues[res] = QubitTransitions[qubitValues[parent]][gate];
            }
            // Not a valid FSM transition gate
            else {
                qubitValues[res] = QubitState::NOT_A_BASIS;
            }
            return;
        });
	}

	llvm::DenseMap<Value, QubitState> getQubitValues(){
		return qubitValues;
	}

// Function to convert enum values to strings
static std::string QubitState2String(QubitState state)
{
    switch (state) {
    case QubitState::ZERO:
        return "ZERO";
    case QubitState::ONE:
        return "ONE";
    case QubitState::PLUS:
        return "PLUS";
    case QubitState::MINUS:
        return "MINUS";
    case QubitState::LEFT:
        return "LEFT";
    case QubitState::RIGHT:
        return "RIGHT";
    case QubitState::NOT_A_BASIS:
        return "NOT_A_BASIS";
    }
}

bool isZero(QubitState qs){
	return qs == QubitState::ZERO;
}

bool isOne(QubitState qs){
	return qs == QubitState::ONE;
}

bool isPlus(QubitState qs){
	return qs == QubitState::PLUS;
}

bool isMinus(QubitState qs){
	return qs == QubitState::MINUS;
}

bool isLeft(QubitState qs){
	return qs == QubitState::LEFT;
}

bool isRight(QubitState qs){
	return qs == QubitState::RIGHT;
}

private:
    // The object `qubitValues` contains all the analysis results
    // It is a map of the form 
    // <mlir Value representing a qubit, its abstract QubitState>
	llvm::DenseMap<Value, QubitState> qubitValues;
};

} // namespace catalyst